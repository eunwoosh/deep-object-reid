# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import numpy as np

from openvino.model_zoo.model_api.models.classification import Classification
from openvino.model_zoo.model_api.models.types import NumericalValue, ListValue, StringValue, BooleanValue


class NewClassification(Classification):
    __model__ = 'new_classification'

    def __init__(self, model_adapter, configuration=None, preload=False):
        super().__init__(model_adapter, configuration, preload)
        self._check_io_number(1, 1)
        if self.path_to_labels:
            self.labels = self._load_labels(self.path_to_labels)

        self.output_blob_name = self._get_outputs()

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'multilabel': BooleanValue(default_value=False),
            'topk': NumericalValue(value_type=int, default_value=1),
            'labels': ListValue(description="List of class labels"),
            'path_to_labels': StringValue(description="Path to file with labels. Overrides the labels, if they sets via 'labels' parameter")
        })

        return parameters

    def postprocess(self, outputs, meta):
        outputs = outputs[self.output_blob_name].squeeze()
        self.activate = False
        if not np.isclose(np.sum(outputs), 1.0, atol=0.01):
            self.activate = True

        if self.multilabel:
            return get_multilabel_predictions(outputs, activate=self.activate)
        else:
            return get_multiclass_predictions(outputs, topk=self.topk, activate=self.activate)


def sigmoid_numpy(x: np.ndarray):
    return 1. / (1. + np.exp(-1. * x))


def softmax_numpy(x: np.ndarray):
    x = np.exp(x)
    x /= np.sum(x)
    return x


def get_multiclass_predictions(logits: np.ndarray, topk: int, activate: bool = True):

    if activate:
        logits = softmax_numpy(logits)
    indices = np.argpartition(logits, -topk)[-topk:]
    scores = logits[indices]
    desc_order = scores.argsort()[::-1]
    scores = scores[desc_order]
    indices = indices[desc_order]
    return list(zip(indices, scores))


def get_multilabel_predictions(logits: np.ndarray, pos_thr: float = 0.5, activate: bool = True):
    if activate:
        logits = sigmoid_numpy(logits)
    scores = []
    indices = []
    for i in range(logits.shape[0]):
        if logits[i] > pos_thr:
            indices.append(i)
            scores.append(logits[i])

    return list(zip(indices, scores))