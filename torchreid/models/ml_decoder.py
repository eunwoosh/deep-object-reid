from typing import Optional

import torch
from torch import nn, Tensor
from torch.nn.modules.transformer import _get_activation_fn

from torchreid.models.common import ModelInterface
from torch.cuda.amp import autocast

def build_ml_decoder_model(backbone, num_classes=80, num_of_groups=-1, decoder_embedding=768, **kwargs):
    """Create a model
    """
    num_features = backbone.num_features
    if num_classes == -1:
        num_classes = backbone.num_classes
    # loading ML decoder model
    if hasattr(backbone, "model"): # timm models
        backbone.model.classifier = None
    else:
        backbone.classifier = None
    model = MLDecoder(backbone, num_classes=num_classes, initial_num_features=num_features, num_of_groups=num_of_groups,
                      decoder_embedding=decoder_embedding, **kwargs)
    return model


def add_ml_decoder_head(model, num_classes, num_of_groups=-1, decoder_embedding=-1):
    if num_classes == -1:
        num_classes = model.num_classes
    num_features = model.num_features
    model.glob_feature_vector = None
    if hasattr(model, "model"): # timm models
        model.model.classifier = MLDecoder(num_classes=num_classes, initial_num_features=num_features, num_of_groups=num_of_groups,
                                           decoder_embedding=decoder_embedding)
    else:
        model.classifier = MLDecoder(num_classes=num_classes, initial_num_features=num_features, num_of_groups=num_of_groups,
                                     decoder_embedding=decoder_embedding)

    return model


class TransformerDecoderLayerOptimal(nn.Module):
    def __init__(self, d_model, nhead=8, dim_feedforward=2048, dropout=0.1, activation="relu",
                 layer_norm_eps=1e-5) -> None:
        super(TransformerDecoderLayerOptimal, self).__init__()
        self.norm1 = nn.LayerNorm(d_model, eps=layer_norm_eps)
        self.dropout = nn.Dropout(dropout)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

        self.multihead_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)

        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm2 = nn.LayerNorm(d_model, eps=layer_norm_eps)
        self.norm3 = nn.LayerNorm(d_model, eps=layer_norm_eps)

        self.activation = _get_activation_fn(activation)

    def __setstate__(self, state):
        if 'activation' not in state:
            state['activation'] = torch.nn.functional.relu
        super(TransformerDecoderLayerOptimal, self).__setstate__(state)

    def forward(self, tgt: Tensor, memory: Tensor, tgt_mask: Optional[Tensor] = None,
                memory_mask: Optional[Tensor] = None,
                tgt_key_padding_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None) -> Tensor:
        tgt = tgt + self.dropout1(tgt)
        tgt = self.norm1(tgt)
        tgt2 = self.multihead_attn(tgt, memory, memory)[0]
        tgt = tgt + self.dropout2(tgt2)
        tgt = self.norm2(tgt)
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt))))
        tgt = tgt + self.dropout3(tgt2)
        tgt = self.norm3(tgt)
        return tgt


# @torch.jit.script
# class ExtrapClasses(object):
#     def __init__(self, num_queries: int, group_size: int):
#         self.num_queries = num_queries
#         self.group_size = group_size
#
#     def __call__(self, h: torch.Tensor, class_embed_w: torch.Tensor, class_embed_b: torch.Tensor, out_extrap:
#     torch.Tensor):
#         # h = h.unsqueeze(-1).expand(-1, -1, -1, self.group_size)
#         h = h[..., None].repeat(1, 1, 1, self.group_size) # torch.Size([bs, 5, 768, groups])
#         w = class_embed_w.view((self.num_queries, h.shape[2], self.group_size))
#         out = (h * w).sum(dim=2) + class_embed_b
#         out = out.view((h.shape[0], self.group_size * self.num_queries))
#         return out

@torch.jit.script
class GroupFC(object):
    def __init__(self, embed_len_decoder: int):
        self.embed_len_decoder = embed_len_decoder

    def __call__(self, h: torch.Tensor, duplicate_pooling: torch.Tensor, out_extrap: torch.Tensor):
        for i in range(h.shape[1]):
            h_i = h[:, i, :]
            if len(duplicate_pooling.shape)==3:
                w_i = duplicate_pooling[i, :, :]
            else:
                w_i = duplicate_pooling
            out_extrap[:, i, :] = torch.matmul(h_i, w_i)


class MLDecoder(ModelInterface):
    def __init__(self, backbone, num_classes, num_of_groups=-1, decoder_embedding=768,
                 initial_num_features=2048, **kwargs):
        super().__init__(num_classes=num_classes, **kwargs)
        self.backbone = backbone
        embed_len_decoder = 100 if num_of_groups < 0 else num_of_groups
        if embed_len_decoder > self.num_classes:
            embed_len_decoder = self.num_classes

        # switching to 768 initial embeddings
        decoder_embedding = 768 if decoder_embedding < 0 else decoder_embedding
        embed_standart = nn.Linear(initial_num_features, decoder_embedding)

        # non-learnable queries
        query_embed = nn.Embedding(embed_len_decoder, decoder_embedding)
        query_embed.requires_grad_(False)

        # decoder
        decoder_dropout = 0.1
        num_layers_decoder = 1
        dim_feedforward = 2048
        layer_decode = TransformerDecoderLayerOptimal(d_model=decoder_embedding,
                                                      dim_feedforward=dim_feedforward, dropout=decoder_dropout)
        self.decoder = nn.TransformerDecoder(layer_decode, num_layers=num_layers_decoder)
        self.decoder.embed_standart = embed_standart
        self.decoder.query_embed = query_embed

        # group fully-connected
        self.decoder.num_classes = self.num_classes
        self.decoder.duplicate_factor = int(self.num_classes / embed_len_decoder + 0.999)
        self.decoder.duplicate_pooling = torch.nn.Parameter(
            torch.Tensor(embed_len_decoder, decoder_embedding, self.decoder.duplicate_factor))
        self.decoder.duplicate_pooling_bias = torch.nn.Parameter(torch.Tensor(self.num_classes))
        torch.nn.init.xavier_normal_(self.decoder.duplicate_pooling)
        torch.nn.init.constant_(self.decoder.duplicate_pooling_bias, 0)
        self.decoder.group_fc = GroupFC(embed_len_decoder)
        self.train_wordvecs = None
        self.test_wordvecs = None

    def forward(self, x):
        with autocast(enabled=self.mix_precision):
            x = self.backbone(x, return_featuremaps=True)
            if len(x.shape) == 4:  # [bs,2048, 7,7]
                embedding_spatial = x.flatten(2).transpose(1, 2)
            else:  # [bs, 197,468]
                embedding_spatial = x
            embedding_spatial_786 = self.decoder.embed_standart(embedding_spatial)
            embedding_spatial_786 = torch.nn.functional.relu(embedding_spatial_786, inplace=True)

            bs = embedding_spatial_786.shape[0]
            query_embed = self.decoder.query_embed.weight
            # tgt = query_embed.unsqueeze(1).repeat(1, bs, 1)
            tgt = query_embed.unsqueeze(1).expand(-1, bs, -1)  # no allocation of memory with expand
            h = self.decoder(tgt, embedding_spatial_786.transpose(0, 1))  # [embed_len_decoder, batch, 768]
            h = h.transpose(0, 1)

            out_extrap = torch.zeros(h.shape[0], h.shape[1], self.decoder.duplicate_factor, device=h.device, dtype=h.dtype)
            self.decoder.group_fc(h, self.decoder.duplicate_pooling, out_extrap)
            h_out = out_extrap.flatten(1)[:, :self.decoder.num_classes]
            h_out += self.decoder.duplicate_pooling_bias
            logits = h_out

            if self.similarity_adjustment:
                logits = self.sym_adjust(logits, self.amb_t)

            if not self.training:
                return [logits]

            elif self.loss in ['asl', 'bce', 'am_binary']:
                    out_data = [logits]
            else:
                raise KeyError("Unsupported loss: {}".format(self.loss))

            return tuple(out_data)