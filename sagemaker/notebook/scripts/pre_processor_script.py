# MIT No Attribution

# Copyright 2022 Amazon Web Services

# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from __future__ import print_function

import time
import sys
from io import StringIO
import os
import shutil # CAN I REMOVE THIS?

import argparse
import csv
import json
import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer, make_column_selector
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import Binarizer, StandardScaler, OneHotEncoder

from sagemaker_containers.beta.framework import (
    content_types, encoders, env, modules, transformer, worker)

feature_columns_names = [
    'ax', # Std calc, represents the acceleration upon the x axis which is the west to east axis in meters per second squared (m/s²)
    'ay', # Std calc, represents the acceleration upon the y axis which is the south to north axis in meters per second squared (m/s²)
    'az', # Std calc, represents the acceleration upon the z axis which is the down to up axis in meters per second squared (m/s²)
    'aigx', # Std calc, represents the acceleration including gravity upon the x axis which is the west to east axis in meters per second squared (m/s²)
    'aigy', # Std calc, represents the acceleration including gravity upon the y axis which is the south to north axis in meters per second squared (m/s²)
    'aigz', # Std calc, represents the acceleration including gravity upon the z axis which is the down to up axis in meters per second squared (m/s²)
    'rralpha', # Std calc, rate at which the device is rotating about its Z axis; that is, being twisted about a line perpendicular to the screen.
    'rrbeta', # Std calc, rate at which the device is rotating about its X axis; that is, front to back.
    'rrgamma'] # Std calc, rate at which the device is rotating about its Y axis; that is, side to side.

label_column = 'label'

feature_columns_dtype = {
    'ax': "float64",
    'ay': "float64",
    'az': "float64",
    'aigx': "float64",
    'aigy': "float64",
    'aigz': "float64",
    'rralpha': "float64",
    'rrbeta': "float64",
    'rrgamma': "float64"
}

label_column_dtype = {'label': "float64"} # Name of the activity, 'Sit', 'Walk', 'Run', etc...

def merge_two_dicts(x, y):
    z = x.copy()   # start with x's keys and values
    z.update(y)    # modifies z with y's keys and values & returns None
    return z

if __name__ == '__main__':

    parser = argparse.ArgumentParser()

    # Sagemaker specific arguments. Defaults are set in the environment variables.
    parser.add_argument('--output-data-dir', type=str, default=os.environ['SM_OUTPUT_DATA_DIR'])
    parser.add_argument('--model-dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--train', type=str, default=os.environ['SM_CHANNEL_TRAIN'])

    args = parser.parse_args()

    # Take the set of files and read them all into a single pandas dataframe
    input_files = [ os.path.join(args.train, file) for file in os.listdir(args.train) ]
    if len(input_files) == 0:
        raise ValueError(('There are no files in {}.\n' +
                          'This usually indicates that the channel ({}) was incorrectly specified,\n' +
                          'the data specification in S3 was incorrectly specified or the role specified\n' +
                          'does not have permission to access the data.').format(args.train, "train"))

    raw_data = [ pd.read_csv(
        file, 
        header=0, 
        names=feature_columns_names + [label_column],
        dtype=merge_two_dicts(label_column_dtype, feature_columns_dtype)) for file in input_files ]
    concat_data = pd.concat(raw_data)

    # Labels should not be preprocessed. predict_fn will reinsert the labels after featurizing.
    concat_data.drop(label_column, axis=1, inplace=True)

    # This section is adapted from the scikit-learn example of using preprocessing pipelines:
    # https://scikit-learn.org/stable/auto_examples/compose/plot_column_transformer_mixed_types.html

    numeric_transformer = make_pipeline(
        SimpleImputer(strategy='median'),
        StandardScaler())

    preprocessor = ColumnTransformer(transformers=[
            ("num", numeric_transformer, make_column_selector())])

    preprocessor.fit(concat_data)

    joblib.dump(preprocessor, os.path.join(args.model_dir, "model.joblib"))

    print("saved model!")


def input_fn(input_data, content_type):
    """Parse input data payload

    We currently only take csv input. Since we need to process both labelled
    and unlabelled data we first determine whether the label column is present
    by looking at how many columns were provided.
    """
    if content_type == 'text/csv':
        # Read the raw input data as CSV.
        df = pd.read_csv(StringIO(input_data), 
                         header=0)

        if len(df.columns) == len(feature_columns_names) + 1:
            # This is a labelled example, includes the ring label
            df.columns = feature_columns_names + [label_column]
        elif len(df.columns) == len(feature_columns_names):
            # This is an unlabelled example.
            df.columns = feature_columns_names

        return df
    else:
        raise ValueError("{} not supported by script!".format(content_type))


def output_fn(prediction, accept):    
    """Format prediction output

    The default accept/content-type between containers for serial inference is JSON.
    We also want to set the ContentType or mimetype as the same value as accept so the next
    container can read the response payload correctly.
    """
    if accept == "application/json":
        print("output_fs called with application/json")
        instances = []
        for row in prediction.tolist():
            instances.append({"features": row})

        json_output = {"instances": instances}

        return worker.Response(json.dumps(json_output), mimetype=accept)
    elif accept == 'text/csv':
        print("output_fs called with text/csv")
        return worker.Response(encoders.encode(prediction, accept), mimetype=accept)
    else:
        raise RuntimeException("{} accept type is not supported by this script.".format(accept))


def predict_fn(input_data, model):
    """Preprocess input data

    We implement this because the default predict_fn uses .predict(), but our model is a preprocessor
    so we want to use .transform().

    The output is returned in the following order:

        rest of features either one hot encoded or standardized
    """        
    
    features = model.transform(input_data)

    if label_column in input_data:
        # Return the label (as the first column) and the set of features.
        return np.insert(features, 0, input_data[label_column], axis=1)
    else:
        # Return only the set of features
        return features


def model_fn(model_dir):
    """Deserialize fitted model
    """
    preprocessor = joblib.load(os.path.join(model_dir, "model.joblib"))
    return preprocessor
