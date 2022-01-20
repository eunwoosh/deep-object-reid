"""
 Copyright (c) 2020-2021 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import os.path as osp
from argparse import REMAINDER, ArgumentDefaultsHelpFormatter, ArgumentParser

import numpy as np
import torch
import torch.nn as nn
from scripts.default_config import get_default_config, model_kwargs, merge_from_files_with_base

import torchreid
from torchreid.utils import load_pretrained_weights


def collect_conv_layers(model):
    conv_layers = []
    for name, m in model.named_modules():
        if isinstance(m, nn.Conv2d):
            weight = m.weight.detach().cpu().numpy()
            bias = m.bias.detach().cpu().numpy() if m.bias is not None else 0.0
            shape = weight.shape

            assert len(shape) == 4
            if shape[2] == shape[3] == 1:
                kernel_type = '1x1'
            elif shape[1] == 1:
                kernel_type = 'dw'
            else:
                kernel_type = 'reg'

            conv_layers.append(dict(
                name=name,
                type=kernel_type,
                weight=weight,
                bias=bias,
                updated=False,
            ))
        elif isinstance(m, nn.BatchNorm2d):
            assert len(conv_layers) > 0

            last_conv = conv_layers[-1]
            assert not last_conv['updated']

            alpha = m.weight.detach().cpu().numpy()
            beta = m.bias.detach().cpu().numpy()
            running_mean = m.running_mean.detach().cpu().numpy()
            running_var = m.running_var.detach().cpu().numpy()

            scales = (alpha / np.sqrt(running_var + 1e-5)).reshape([-1, 1, 1, 1])
            last_conv['weight'] = scales * last_conv['weight']
            last_conv['bias'] = scales * (last_conv['bias'] - running_mean) + beta
            last_conv['updated'] = True

    return conv_layers


def show_stat(conv_layers, max_scale=5.0, max_similarity=0.5, sim_percentile=95):
    invalid_weight_scales = []
    invalid_bias_scales = []
    invalid_sim = []
    for conv in conv_layers:
        name = conv['name']
        weights = conv['weight']
        bias = conv['bias']
        kernel_type = conv['type']
        if conv['updated']:
            kernel_type += ', fused'

        num_filters = weights.shape[0]
        filters = weights.reshape([num_filters, -1])

        norms = np.sqrt(np.sum(np.square(filters), axis=-1))
        min_norm, max_norm = np.min(norms), np.max(norms)
        median_norm = np.median(norms)
        scale = max_norm / min_norm

        if num_filters <= filters.shape[1] and 'gate.fc' not in name:
            norm_filters = filters / norms.reshape([-1, 1])
            similarities = np.matmul(norm_filters, np.transpose(norm_filters))

            similarities = np.abs(similarities[np.triu_indices(similarities.shape[0], k=1)])

            num_invalid = np.sum(similarities > max_similarity)
            num_total = len(similarities)
            if num_invalid > 0:
                sim = np.percentile(similarities, sim_percentile)
                invalid_sim.append((name, kernel_type, sim, num_invalid, num_total, num_filters))

        scales = max_norm / norms
        num_invalid = np.sum(scales > max_scale)
        if num_invalid > 0 or median_norm < 0.1:
            invalid_weight_scales.append((name, kernel_type, median_norm, scale, num_invalid, num_filters))

        bias_scores = np.abs(bias)
        bias_score = np.percentile(bias_scores, 95)
        if bias_score > 1.0:
            invalid_bias_scales.append((name, kernel_type, bias_score))

    if len(invalid_weight_scales) > 0:
        print('\nFound {} layers with invalid weight norm fraction (max/cur > {}):'
              .format(len(invalid_weight_scales), max_scale))
        for name, kernel_type, median_norm, scale, num_invalid, num_filters in invalid_weight_scales:
            print('   - {} ({}): {:.3f} (median={:.3f} invalid: {} / {})'
                  .format(name, kernel_type, scale, median_norm, num_invalid, num_filters))
    else:
        print('\nThere are no layers with invalid weight norm.')

    if len(invalid_bias_scales) > 0:
        print('\nFound {} layers with invalid bias max value (max> {}):'
              .format(len(invalid_bias_scales), 1.0))
        for name, kernel_type, scale in invalid_bias_scales:
            print('   - {} ({}): {:.3f}'
                  .format(name, kernel_type, scale))
    else:
        print('\nThere are no layers with invalid bias.')

    if len(invalid_sim) > 0:
        print('\nFound {} layers with invalid similarity (value > {}):'
              .format(len(invalid_sim), max_similarity))
        for name, kernel_type, sim, num_invalid, num_total, num_filters in invalid_sim:
            print('   - {} ({}): {:.3f} (invalid: {} / {} size={})'
                  .format(name, kernel_type, sim, num_invalid, num_total, num_filters))
    else:
        print('\nThere are no layers with invalid similarity.')


def main():
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config', '-c', type=str, required=True)
    parser.add_argument('opts', default=None, nargs=REMAINDER)
    args = parser.parse_args()

    assert osp.exists(args.config)

    cfg = get_default_config()
    cfg.use_gpu = torch.cuda.is_available()
    merge_from_files_with_base(cfg, args.config)
    cfg.merge_from_list(args.opts)
    cfg.freeze()

    model = torchreid.models.build_model(**model_kwargs(cfg, [0, 0]))
    load_pretrained_weights(model, cfg.model.load_weights)

    conv_layers = collect_conv_layers(model)
    show_stat(conv_layers)


if __name__ == '__main__':
    main()
