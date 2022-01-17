# Copyright (c) 2018-2021 Kaiyang Zhou
# SPDX-License-Identifier: MIT
#
# Copyright (c) 2018 davidtvs
# SPDX-License-Identifier: MIT
#
# Copyright (C) 2020-2021 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import absolute_import, division, print_function
import copy
import errno
import json
import os
import os.path as osp
import random
import sys
import time
import warnings
import yaml

import numpy as np
import PIL
import torch
import cv2 as cv
from PIL import Image

__all__ = [
    'mkdir_if_missing', 'check_isfile', 'read_yaml',
    'set_random_seed', "worker_init_fn", 'download_url', 'read_image', 'collect_env_info',
    'get_model_attr', 'StateCacher', 'random_image'
]


def mkdir_if_missing(dirname):
    """Creates dirname if it is missing."""
    if not osp.exists(dirname):
        try:
            os.makedirs(dirname)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

def check_isfile(fpath):
    """Checks if the given path is a file.

    Args:
        fpath (str): file path.

    Returns:
       bool
    """
    isfile = osp.isfile(fpath)
    return isfile

def set_random_seed(seed, deterministic=False):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    if deterministic:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)

def worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)
    random.seed(random.getstate()[1][0] + worker_id)

def download_url(url, dst):
    """Downloads file from a url to a destination.

    Args:
        url (str): url to download file.
        dst (str): destination path.
    """
    from six.moves import urllib
    print('* url="{}"'.format(url))
    print('* destination="{}"'.format(dst))

    def _reporthook(count, block_size, total_size):
        global start_time
        if count == 0:
            start_time = time.time()
            return
        duration = time.time() - start_time
        progress_size = int(count * block_size)
        speed = int(progress_size / (1024*duration))
        percent = int(count * block_size * 100 / total_size)
        sys.stdout.write(
            '\r...%d%%, %d MB, %d KB/s, %d seconds passed' %
            (percent, progress_size / (1024*1024), speed, duration)
        )
        sys.stdout.flush()

    urllib.request.urlretrieve(url, dst, _reporthook)  # nosec
    sys.stdout.write('\n')


def read_image(path, grayscale=False):
    """Reads image from path using ``Open CV``.

    Args:
        path (str): path to an image.
        grayscale (bool): load grayscale image

    Returns:
        Numpy image
    """

    got_img = False
    if not osp.exists(path):
        raise IOError('"{}" does not exist'.format(path))

    while not got_img:
        try:
            img = cv.cvtColor(cv.imread(path, cv.IMREAD_COLOR), cv.COLOR_BGR2RGB)
            got_img = True
        except IOError:
            print('IOError occurred when reading "{}".'.format(path))

    return img


def random_image(height, width):
    input_size = (height, width, 3)
    img = np.random.rand(*input_size).astype(np.float32)
    img = np.uint8(img * 255)

    return img


def get_model_attr(model, attr):
    if hasattr(model, 'module'):
        model = model.module
    if hasattr(model, 'nncf_module'):
        return getattr(model.nncf_module, attr)
    return getattr(model, attr)


class StateCacher(object):
    def __init__(self, in_memory, cache_dir=None):
        self.in_memory = in_memory
        self.cache_dir = cache_dir

        if self.cache_dir is None:
            import tempfile

            self.cache_dir = tempfile.gettempdir()
        else:
            if not os.path.isdir(self.cache_dir):
                raise ValueError("Given `cache_dir` is not a valid directory.")

        self.cached = {}

    def store(self, key, state_dict):
        if self.in_memory:
            self.cached.update({key: copy.deepcopy(state_dict)})
        else:
            fn = os.path.join(self.cache_dir, "state_{}_{}.pt".format(key, id(self)))
            self.cached.update({key: fn})
            torch.save(state_dict, fn)

    def retrieve(self, key):
        if key not in self.cached:
            raise KeyError("Target {} was not cached.".format(key))

        if self.in_memory:
            return self.cached.get(key)
        else:
            fn = self.cached.get(key)
            if not os.path.exists(fn):
                raise RuntimeError(
                    "Failed to load state in {}. File doesn't exist anymore.".format(fn)
                )
            state_dict = torch.load(fn, map_location=lambda storage, location: storage)
            return state_dict

    def __del__(self):
        """Check whether there are unused cached files existing in `cache_dir` before
        this instance being destroyed."""

        if self.in_memory:
            return

        for k in self.cached:
            if os.path.exists(self.cached[k]):
                os.remove(self.cached[k])


class EvalModeSetter:
    def __init__(self, module, m_type):
        self.modules = module
        if not isinstance(self.modules, (tuple, list)):
            self.modules = [self.modules]

        self.modes_storage = [{} for _ in range(len(self.modules))]

        self.m_types = m_type
        if not isinstance(self.m_types, (tuple, list)):
            self.m_types = [self.m_types]

    def __enter__(self):
        for module_id, module in enumerate(self.modules):
            modes_storage = self.modes_storage[module_id]

            for child_name, child_module in module.named_modules():
                matched = any(isinstance(child_module, m_type) for m_type in self.m_types)
                if matched:
                    modes_storage[child_name] = child_module.training
                    child_module.train(mode=False)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for module_id, module in enumerate(self.modules):
            modes_storage = self.modes_storage[module_id]

            for child_name, child_module in module.named_modules():
                if child_name in modes_storage:
                    child_module.train(mode=modes_storage[child_name])
