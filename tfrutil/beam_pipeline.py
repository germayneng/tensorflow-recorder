# Lint as: python3

# Copyright 2020 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TFRUtil Beam Pipeline.

This file implements the full beam pipeline for TFRUtil.
"""
import datetime
import functools
import logging
import os
from typing import Dict, Union

import apache_beam as beam
import pandas as pd
import tensorflow_transform as tft
from tensorflow_transform import beam as tft_beam

from tfrutil import beam_image
from tfrutil import constants


def _get_job_name(job_label: str = None) -> str:
  """Returns Beam runner job name.

  Args:
    job_label: A user defined string that helps define the job.

  Returns:
    A job name compatible with apache beam runners, including a time stamp to
      insure uniqueness.
  """

  job_name = "tfrutil-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
  if job_label:
    job_label = job_label.replace("_", "-")
    job_name += "-" + job_label

  return job_name


def _get_job_dir(output_path: str, job_name: str) -> str:
  """Returns Beam processing job directory."""

  return os.path.join(output_path, job_name)


def _get_pipeline_options(
    job_name: str, job_dir: str, **popts: Union[bool, str, float]
    ) -> beam.pipeline.PipelineOptions:
  """Returns Beam pipeline options."""

  options_dict = {
      "staging_location": os.path.join(job_dir, "staging"),
      "temp_location": os.path.join(job_dir, "tmp"),
      "job_name": job_name,
      "teardown_policy": "TEARDOWN_ALWAYS",
      "save_main_session": True,
      "pipeline_type_check": False,
  }
  options_dict.update(popts)
  return beam.pipeline.PipelineOptions(flags=[], **options_dict)


def _partition_fn(element: Dict[str, str], unused_num_partitions: int) -> int:
  """Returns index used to partition an element from a PCollection."""
  del unused_num_partitions
  dataset_type = element[constants.SPLIT_KEY].decode("utf-8")
  try:
    index = constants.SPLIT_VALUES.index(dataset_type)
  except ValueError as e:
    logging.warning('Unable to index dataset type %s: %s.',
                    dataset_type, str(e))
    index = constants.DISCARD_INDEX
  return index

def _get_write_to_tfrecord(output_path: str,
                           prefix: str,
                           compress: bool = True,
                           num_shards: int = 0) \
                           -> beam.io.tfrecordio.WriteToTFRecord:
  """Returns `beam.io.tfrecordio.WriteToTFRecord` object.

  This configures a Beam sink to output TFRecord files.

  Args:
    output_path: Directory to output TFRecord files.
    prefix: TFRecord file prefix.
    compress: If True, GZip compress TFRecord files.
    num_shards: Number of file shards to split the TFRecord data.
  """

  path = os.path.join(output_path, prefix)
  suffix = '.tfrecord'
  if compress:
    compression_type = 'gzip'
    suffix += '.gz'
  else:
    compression_type = 'uncompressed'

  return beam.io.tfrecordio.WriteToTFRecord(
      path,
      file_name_suffix=suffix,
      compression_type=compression_type,
      num_shards=num_shards,
  )

def _preprocessing_fn(inputs):
  """TensorFlow Transform preprocessing function."""

  outputs = inputs.copy()
  return outputs


# pylint: disable=too-many-arguments
# pylint: disable=too-many-locals
def run_pipeline(df: pd.DataFrame,
                 job_label: str,
                 runner: str,
                 output_path: str,
                 compression: str,
                 num_shards: int):
  """Runs TFRUtil Beam Pipeline.

  Args:
    df: Pandas Dataframe
    job_label: User description for the beam job.
    runner: Beam Runner: (e.g. DataFlowRunner, DirectRunner).
    output_path: GCS or Local Path for output.
    compression: gzip or None.
    num_shards: Number of shards.

  Note: These inputs must be validated upstream (by client.create_tfrecord())
  """

  job_name = _get_job_name(job_label)
  job_dir = _get_job_dir(output_path, job_name)
  popts = {}  # TODO(mikebernico): consider how/if to pass pipeline options.
  options = _get_pipeline_options(job_name, job_dir, **popts)

  with beam.Pipeline(runner, options=options) as p:
    with tft_beam.Context(temp_dir=os.path.join(job_dir, "tft_tmp")):

      converter = tft.coders.CsvCoder(constants.IMAGE_CSV_COLUMNS,
                                      constants.IMAGE_CSV_METADATA.schema)

      extract_images_fn = beam_image.ExtractImagesDoFn(constants.IMAGE_URI_KEY)

      # Each element in the image_csv_data PCollection will be a dict
      # including the image_csv_columns and the image features created from
      # extract_images_fn.
      image_csv_data = (
          p
          | "ReadFromDataFrame" >> beam.Create(df.values.tolist())
          | "ToCSVRows" >> beam.Map(
              lambda x: ",".join([str(item) for item in x]))
          | "DecodeCSV" >> beam.Map(converter.decode)
          | "ReadImage" >> beam.ParDo(extract_images_fn)
      )

      # Split dataset into train and validation.
      discard_data, train_data, val_data, test_data = (
          image_csv_data | 'SplitDataset' >> beam.Partition(
              _partition_fn, len(constants.SPLIT_VALUES))
      )

      train_dataset = (train_data, constants.RAW_METADATA)
      val_dataset = (val_data, constants.RAW_METADATA)
      test_dataset = (test_data, constants.RAW_METADATA)

      # TensorFlow Transform applied to all datasets.
      transformed_train_dataset, transform_fn = (
          train_dataset
          | 'AnalyzeAndTransformTrain' >> tft_beam.AnalyzeAndTransformDataset(
              _preprocessing_fn))

      transformed_train_data, transformed_metadata = transformed_train_dataset
      transformed_data_coder = tft.coders.ExampleProtoCoder(
          transformed_metadata.schema)

      transformed_val_data, _ = (
          (val_dataset, transform_fn)
          | 'TransformVal' >> tft_beam.TransformDataset()
      )

      transformed_test_data, _ = (
          (test_dataset, transform_fn)
          | 'TransformTest' >> tft_beam.TransformDataset()
      )

      # Sinks for TFRecords and metadata.
      tfr_writer = functools.partial(_get_write_to_tfrecord,
                                     output_path=job_dir,
                                     compress=compression,
                                     num_shards=num_shards)

      _ = (
          transformed_train_data
          | 'EncodeTrainData' >> beam.Map(transformed_data_coder.encode)
          | 'WriteTrainData' >> tfr_writer(prefix="train"))

      _ = (
          transformed_val_data
          | 'EncodeValData' >> beam.Map(transformed_data_coder.encode)
          | 'WriteValData' >> tfr_writer(prefix="val"))

      _ = (
          transformed_test_data
          | 'EncodeTestData' >> beam.Map(transformed_data_coder.encode)
          | 'WriteTestData' >> tfr_writer(prefix="test"))

      _ = (
          discard_data
          | 'DiscardDataWriter' >> beam.io.WriteToText(
              os.path.join(job_dir + "discarded-data")))


      # Output transform function
      transformed_function_path = os.path.join(job_dir, 'transform_function')
      _ = (
          transform_fn | 'WriteTransformFn' >> tft_beam.WriteTransformFn(
              transformed_function_path))

      # Output metdata
      transformed_metadata_path = os.path.join(job_dir, 'transformed_metadata')
      _ = (
          transformed_metadata | 'WriteMetadata' >> tft_beam.WriteMetadata(
              transformed_metadata_path, pipeline=p))
