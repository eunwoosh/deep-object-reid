import time
import os
import json
from os import path as osp
import importlib
import tempfile
import subprocess
from typing import List

import cv2 as cv
import numpy as np

from ote_sdk.entities.image import Image
from ote_sdk.entities.shapes.rectangle import Rectangle
from ote_sdk.entities.scored_label import ScoredLabel
from ote_sdk.entities.label import LabelEntity, Domain
from ote_sdk.entities.annotation import Annotation, AnnotationSceneEntity, AnnotationSceneKind
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.dataset_item import DatasetItemEntity
from ote_sdk.entities.label_schema import (LabelGroup, LabelGroupType,
                                           LabelSchemaEntity)
from ote_sdk.entities.subset import Subset
from ote_sdk.entities.train_parameters import UpdateProgressCallback
from ote_sdk.usecases.reporting.time_monitor_callback import \
    TimeMonitorCallback


class ClassificationDatasetAdapter(DatasetEntity):
    def __init__(self,
                 train_ann_file=None,
                 train_data_root=None,
                 val_ann_file=None,
                 val_data_root=None,
                 test_ann_file=None,
                 test_data_root=None,
                 **kwargs):
        self.data_roots = {}
        self.ann_files = {}
        self.multilabel = False
        if train_data_root:
            self.data_roots[Subset.TRAINING] = train_data_root
            self.ann_files[Subset.TRAINING] = train_ann_file
        if val_data_root:
            self.data_roots[Subset.VALIDATION] = val_data_root
            self.ann_files[Subset.VALIDATION] = val_ann_file
        if test_data_root:
            self.data_roots[Subset.TESTING] = test_data_root
            self.ann_files[Subset.TESTING] = test_ann_file
        self.annotations = {}
        for k, v in self.data_roots.items():
            if v:
                self.data_roots[k] = osp.abspath(v)
                if self.ann_files[k] and '.json' in self.ann_files[k] and osp.isfile(self.ann_files[k]):
                    self.data_roots[k] = osp.dirname(self.ann_files[k])
                    self.multilabel = True
                    self.annotations[k] = self._load_annotation_multilabel(self.ann_files[k], self.data_roots[k])
                else:
                    self.annotations[k] = self._load_annotation(self.data_roots[k])
                    assert not self.multilabel

        self.label_map = None
        self.labels = None
        self._set_labels_obtained_from_annotation()
        self.project_labels = [LabelEntity(name=name, domain=Domain.CLASSIFICATION, is_empty=False) for i, name in
                               enumerate(self.labels)]

        dataset_items = []
        for subset, subset_data in self.annotations.items():
            for data_info in subset_data[0]:
                image = Image(file_path=data_info[0])
                labels = [ScoredLabel(label=self._label_name_to_project_label(label_name),
                                      probability=1.0) for label_name in data_info[1]]
                shapes = [Annotation(Rectangle.generate_full_box(), labels)]
                annotation_scene = AnnotationSceneEntity(kind=AnnotationSceneKind.ANNOTATION,
                                                         annotations=shapes)
                dataset_item = DatasetItemEntity(image, annotation_scene, subset=subset)
                dataset_items.append(dataset_item)

        super().__init__(items=dataset_items, **kwargs)

    @staticmethod
    def _load_annotation_multilabel(annot_path, data_dir):
        out_data = []
        with open(annot_path) as f:
            annotation = json.load(f)
            classes = sorted(annotation['classes'])
            class_to_idx = {classes[i]: i for i in range(len(classes))}
            images_info = annotation['images']
            img_wo_objects = 0
            for img_info in images_info:
                rel_image_path, img_labels = img_info
                full_image_path = osp.join(data_dir, rel_image_path)
                labels_idx = [lbl for lbl in img_labels if lbl in class_to_idx]
                assert full_image_path
                if not labels_idx:
                    img_wo_objects += 1
                out_data.append((full_image_path, tuple(labels_idx), 0, 0, '', -1, -1))
        if img_wo_objects:
            print(f'WARNING: there are {img_wo_objects} images without labels and will be treated as negatives')
        return out_data, class_to_idx

    @staticmethod
    def _load_annotation(data_dir, filter_classes=None):
        ALLOWED_EXTS = ('.jpg', '.jpeg', '.png', '.gif')
        def is_valid(filename):
            return not filename.startswith('.') and filename.lower().endswith(ALLOWED_EXTS)

        def find_classes(folder, filter_names=None):
            if filter_names:
                classes = [d.name for d in os.scandir(folder) if d.is_dir() and d.name in filter_names]
            else:
                classes = [d.name for d in os.scandir(folder) if d.is_dir()]
            classes.sort()
            class_to_idx = {classes[i]: i for i in range(len(classes))}
            return class_to_idx

        class_to_idx = find_classes(data_dir, filter_classes)

        out_data = []
        for target_class in sorted(class_to_idx.keys()):
            # class_index = class_to_idx[target_class]
            target_dir = osp.join(data_dir, target_class)
            if not osp.isdir(target_dir):
                continue
            for root, _, fnames in sorted(os.walk(target_dir, followlinks=True)):
                for fname in sorted(fnames):
                    path = osp.join(root, fname)
                    if is_valid(path):
                        out_data.append((path, (target_class, ), 0, 0, '', -1, -1))

        if not out_data:
            print('Failed to locate images in folder ' + data_dir + f' with extensions {ALLOWED_EXTS}')

        return out_data, class_to_idx

    def _set_labels_obtained_from_annotation(self):
        self.labels = None
        self.label_map = {}
        for subset in self.data_roots:
            self.label_map = self.annotations[subset][1]
            labels = list(self.annotations[subset][1].keys())
            if self.labels and self.labels != labels:
                raise RuntimeError('Labels are different from annotation file to annotation file.')
            self.labels = labels
        assert self.labels is not None

    def _label_name_to_project_label(self, label_name):
        return [label for label in self.project_labels if label.name == label_name][0]

    def is_multilabel(self):
        return self.multilabel


def get_empty_label(task_environment) -> LabelEntity:
    empty_candidates = list(set(task_environment.get_labels(include_empty=True)) -
                            set(task_environment.get_labels(include_empty=False)))
    if empty_candidates:
        return empty_candidates[0]
    return None


def generate_label_schema(not_empty_labels, multilabel=False):
    assert len(not_empty_labels) > 1

    label_schema = LabelSchemaEntity()
    if multilabel:
        emptylabel = LabelEntity(name="Empty label", is_empty=True, domain=Domain.CLASSIFICATION)
        empty_group = LabelGroup(name="empty", labels=[emptylabel], group_type=LabelGroupType.EMPTY_LABEL)
        single_groups = []
        for label in not_empty_labels:
            single_groups.append(LabelGroup(name=label.name, labels=[label], group_type=LabelGroupType.EXCLUSIVE))
            label_schema.add_group(single_groups[-1])
        label_schema.add_group(empty_group, exclusive_with=single_groups)
    else:
        main_group = LabelGroup(name="labels", labels=not_empty_labels, group_type=LabelGroupType.EXCLUSIVE)
        label_schema.add_group(main_group)
    return label_schema


class OTEClassificationDataset():
    def __init__(self, ote_dataset: DatasetEntity, labels, multilabel=False,
                 keep_empty_label=False):
        super().__init__()
        self.ote_dataset = ote_dataset
        self.multilabel = multilabel
        self.labels = labels
        self.annotation = []
        self.keep_empty_label = keep_empty_label
        self.label_names = [label.name for label in self.labels]

        for i, _ in enumerate(self.ote_dataset):
            class_indices = []
            item_labels = self.ote_dataset[i].get_roi_labels(self.labels,
                                                             include_empty=self.keep_empty_label)
            if item_labels:
                for ote_lbl in item_labels:
                    class_indices.append(self.label_names.index(ote_lbl.name))
            else:
                class_indices.append(0)

            if self.multilabel:
                self.annotation.append({'label': tuple(class_indices)})
            else:
                self.annotation.append({'label': class_indices[0]})

    def __getitem__(self, idx):
        sample = self.ote_dataset[idx].numpy  # This returns 8-bit numpy array of shape (height, width, RGB)
        label = self.annotation[idx]['label']
        return {'img': sample, 'label': label}

    def __len__(self):
        return len(self.annotation)

    def get_annotation(self):
        return self.annotation

    def get_classes(self):
        return self.label_names


def get_task_class(path):
    module_name, class_name = path.rsplit('.', 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def reload_hyper_parameters(model_template):
    """ This function copies template.yaml file and its configuration.yaml dependency to temporal folder.
        Then it re-loads hyper parameters from copied template.yaml file.
        This function should not be used in general case, it is assumed that
        the 'configuration.yaml' should be in the same folder as 'template.yaml' file.
    """

    template_file = model_template.model_template_path
    template_dir = osp.dirname(template_file)
    temp_folder = tempfile.mkdtemp()
    conf_yaml = [dep.source for dep in model_template.dependencies \
                     if dep.destination == model_template.hyper_parameters.base_path][0]
    conf_yaml = osp.join(template_dir, conf_yaml)
    subprocess.run(f'cp {conf_yaml} {temp_folder}', check=True, shell=True)
    subprocess.run(f'cp {template_file} {temp_folder}', check=True, shell=True)
    model_template.hyper_parameters.load_parameters(osp.join(temp_folder, 'template.yaml'))
    assert model_template.hyper_parameters.data


def set_values_as_default(parameters):
    for v in parameters.values():
        if isinstance(v, dict) and 'value' not in v:
            set_values_as_default(v)
        elif isinstance(v, dict) and 'value' in v:
            if v['value'] != v['default_value']:
                v['value'] = v['default_value']


class TrainingProgressCallback(TimeMonitorCallback):
    def __init__(self, update_progress_callback: UpdateProgressCallback, **kwargs):
        super().__init__(update_progress_callback=update_progress_callback, **kwargs)

    def on_train_batch_end(self, batch, logs=None):
        super().on_train_batch_end(batch, logs)
        self.update_progress_callback(self.get_progress(), score=logs)

    def on_epoch_end(self, epoch, logs=None):
        self.past_epoch_duration.append(time.time() - self.start_epoch_time)
        self.__calculate_average_epoch()
        self.update_progress_callback(self.get_progress(), score=logs)

    def __calculate_average_epoch(self):
        if len(self.past_epoch_duration) > self.epoch_history:
            self.past_epoch_duration.remove(self.past_epoch_duration[0])
        self.average_epoch = sum(self.past_epoch_duration) / len(
            self.past_epoch_duration)


class InferenceProgressCallback(TimeMonitorCallback):
    def __init__(self, num_test_steps, update_progress_callback: UpdateProgressCallback):
        super().__init__(
            num_epoch=0,
            num_train_steps=0,
            num_val_steps=0,
            num_test_steps=num_test_steps,
            update_progress_callback=update_progress_callback)

    def on_test_batch_end(self, batch=None, logs=None):
        super().on_test_batch_end(batch, logs)
        self.update_progress_callback(self.get_progress())


def preprocess_features_for_actmap(features):
    features = np.mean(features, axis=1)
    b, h, w = features.shape
    features = features.reshape(b, h * w)
    features = np.exp(features)
    features /= np.sum(features, axis=1, keepdims=True)
    features = features.reshape(b, h, w)
    return features


def get_actmap(features, output_res):
    am = cv.resize(features, output_res)
    am = 255 * (am - np.min(am)) / (np.max(am) - np.min(am) + 1e-12)
    am = np.uint8(np.floor(am))
    am = cv.applyColorMap(am, cv.COLORMAP_JET)
    return am


def active_score_from_probs(predictions):
    top_idxs = np.argpartition(predictions, -2)[-2:]
    top_probs = predictions[top_idxs]
    return np.max(top_probs) - np.min(top_probs)


def sigmoid_numpy(x: np.ndarray):
    return 1. / (1. + np.exp(-1. * x))


def softmax_numpy(x: np.ndarray):
    x = np.exp(x)
    x /= np.sum(x)
    return x


def get_multiclass_predictions(logits: np.ndarray, labels: List[LabelEntity], activate: bool = True):
    i = np.argmax(logits)
    if activate:
        logits = softmax_numpy(logits)
    return [ScoredLabel(labels[i], probability=float(logits[i]))]


def get_multilabel_predictions(logits: np.ndarray, labels: List[LabelEntity],
                               pos_thr: float = 0.5, activate: bool = True):
    if activate:
        logits = sigmoid_numpy(logits)
    item_labels = []
    for i in range(logits.shape[0]):
        if logits[i] > pos_thr:
            label = ScoredLabel(label=labels[i], probability=float(logits[i]))
            item_labels.append(label)

    return item_labels