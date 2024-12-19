# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import torch
import abc
from abc import ABC

import gc
from deepspeed.ops.op_builder import FPQuantizerBuilder
from deepspeed.accelerator import get_accelerator

fp_quant_module = None


class Quantizer(ABC):
    """
    Abstract Quantizer class that implements quantize/dequantize methods.

    Arguments:
        group_size (int, optional): number of values or elements that are grouped
            together for the quantization process.
    """

    def __init__(self, group_size=512) -> None:
        self.group_size = group_size

    @abc.abstractmethod
    def quantize(self,
                 input,
                 q_bits=8,
                 q_mantisa_bits=3,
                 stochastic_mode=False,
                 return_meta_tensor=False) -> torch.Tensor:
        ...

    @abc.abstractmethod
    def dequantize(self, input_q, fp_out=None, q_bits=8, q_mantisa_bits=3, scale=None) -> torch.Tensor:
        ...


class FP_Quantize(Quantizer):

    def __init__(self, quantization_config) -> None:
        global fp_quant_module
        super().__init__(group_size=quantization_config.group_size)
        if fp_quant_module is None:
            fp_quant_module = FPQuantizerBuilder().load()
        self.is_fallback = fp_quant_module is None
        self.quantize_func = self.quantize_fallback if self.is_fallback else fp_quant_module.quantize
        self.get_scales_func = self.get_scales_fallback if self.is_fallback else fp_quant_module.get_scales
        self.dequantize_func = self.dequantize_fallback if self.is_fallback else fp_quant_module.dequantize
        self.selective_dequantize_func = self.selective_dequantize_fallback if self.is_fallback else fp_quant_module.selective_dequantize
        self.q_config = quantization_config

        self.orig_dtype = None
        self.num_groups = None
        self.input_q = None
        self.scale = None

    def quantize(self,
                 input,
                 q_bits=8,
                 q_mantisa_bits=3,
                 stochastic_mode=False,
                 return_meta_tensor=False) -> torch.Tensor:
        assert input.dtype == torch.bfloat16, "only support bf16 for now"
        if return_meta_tensor:
            assert q_bits == 8, "meta tensor is only supported with q_bit=8"

        self.orig_dtype = input.dtype
        self.orig_shape = input.shape

        if q_bits == 8:
            pass
        elif q_bits == 12:
            q_mantisa_bits = 4
        elif q_bits == 6:
            q_mantisa_bits = 2
        elif q_bits == 4:
            q_mantisa_bits = 1
        else:
            assert (0), \
                f"Missing {q_bits}-quantization, please add the template arguments for the kernel to support this precision!"

        # Adding (group_size - 1) is for padding
        self.num_groups = (input.numel() + self.q_config.group_size - 1) // self.q_config.group_size
        # group_size should be the minimal number between the defined group size and number of elements in tensor.
        group_size = int(min(self.q_config.group_size, input.numel()) * q_bits) // 8
        # CUDA quantization kernel saves the scale as (fp32) inside the quantized tensor for each group
        if not self.is_fallback:
            group_size += 4
        # CUDA quantization kernel allocates tensors as uint8, but handles them as fp8 inside the kernel.
        self.input_q = torch.ones(self.num_groups, group_size, dtype=self.q_config.q_dtype, device=input.device)
        out = self.quantize_func(self.input_q, input, group_size, stochastic_mode, q_bits, q_mantisa_bits)
        if return_meta_tensor:
            if not self.is_fallback:
                data, self.scale = out.split(group_size, dim=-1)
                data = data.contiguous().reshape(input.shape)
            else:
                data = out.contiguous().reshape(input.shape)
            self.scale = self.scale.contiguous()
            del self.input_q
            del out
            gc.collect()
            get_accelerator().empty_cache()
            return data, self.scale

        return out

    def to(self, *args, **kwargs):
        # Intermediate tensors may need to be moved to different devices
        if hasattr(self, 'input_q') and self.input_q is not None:
            self.input_q = self.input_q.to(*args, **kwargs)
        if hasattr(self, 'scale') and self.scale is not None:
            self.scale = self.scale.to(*args, **kwargs)

    def get_scales(self):
        return self.get_scales_func(self.scale, self.num_groups)

    def dequantize(self, input_q, fp_out=None, q_bits=8, q_mantisa_bits=3, scale=None) -> torch.Tensor:
        assert (self.orig_dtype is not None), \
            "[De-quantization Error]: you need to call quantize before dequantizing!"
        fp_out = torch.empty(self.orig_shape, dtype=self.orig_dtype,
                             device=input_q.device) if fp_out is None else fp_out
        if q_bits == 8:
            pass
        elif q_bits == 12:
            q_mantisa_bits = 4
        elif q_bits == 6:
            q_mantisa_bits = 2
        elif q_bits == 4:
            q_mantisa_bits = 1
        else:
            assert (0), \
                f"Missing {q_bits}-dequantization, please add the template arguments for the kernel to support this precision!"

        if scale is not None and not self.is_fallback:
            assert input_q.numel() == fp_out.numel(), \
            f'[De-quantization Error]: quantized data should have the same size as original tensor when scale is not None!'
            input_q = torch.cat([input_q.reshape(-1, self.q_config.group_size), scale], dim=-1).contiguous()
        elif scale is not None and self.is_fallback:
            group_size = int(min(self.q_config.group_size, input_q.numel()) * q_bits) // 8
            input_q = input_q.reshape(-1, group_size)

        self.dequantize_func(fp_out, input_q, self.q_config.group_size, q_mantisa_bits, q_bits - q_mantisa_bits - 1)
        return fp_out

    def selective_dequantize(self,
                             input_q,
                             indexes,
                             fp_out=None,
                             q_bits=8,
                             q_mantisa_bits=3,
                             scale=None) -> torch.Tensor:
        assert (not hasattr(self, 'orig_shape') or len(self.orig_shape) == 3), \
            "Selective-Dequantization works on 3d tensor only! Please reshape the tensor before calling dequantize function."
        assert (self.orig_dtype is not None), \
            "[De-quantization Error]: you need to call quantize before dequantizing!"
        fp_out = torch.empty(
            (indexes.shape[0],
             *self.orig_shape[1:]), dtype=self.orig_dtype, device=input_q.device) if fp_out is None else fp_out
        if q_bits == 8:
            pass
        elif q_bits == 12:
            q_mantisa_bits = 4
        elif q_bits == 6:
            q_mantisa_bits = 2
        elif q_bits == 4:
            q_mantisa_bits = 1
        else:
            assert (0), \
                f"Missing {q_bits}-dequantization, please add the template arguments for the kernel to support this precision!"

        if scale is not None and not self.is_fallback:
            assert input_q.numel() == fp_out.numel(), \
            f'[De-quantization Error]: quantized data should have the same size as original tensor when scale is not None!'
            input_q = torch.cat([input_q.reshape(-1, self.q_config.group_size), scale], dim=-1).contiguous()

        self.selective_dequantize_func(fp_out, input_q, indexes, self.q_config.group_size, q_mantisa_bits,
                                       q_bits - q_mantisa_bits - 1)
        return fp_out

    def selective_dequantize_fallback(self, val_q, scales, indexes, group_size, q_mantisa_bits, q_exponent_bits):
        assert False, "Selective dequantize fallback isn't implemented!"

    def dequantize_fallback(self, fp_out, input_q, group_size, q_mantisa_bits, q_exponent_bits):
        # TODO: [SW-208941] clear gaudi specific code.
        dequant_out = torch.ops.hpu.cast_from_fp8(input_q, (1.0 / self.scale), self.orig_dtype).view(self.orig_shape)
        fp_out.copy_(dequant_out)
        return fp_out

    def quantize_fallback(self, out, val, group_size, stochastic_rounding, q_bits, q_mantisa_bits):
        assert q_bits == 8, "Quantize fallback only supports quantization to FP8"
        assert q_mantisa_bits == 3, "Quantize fallback only supports q_mantissa_bits = 3"
        # TODO: [SW-208941] clear gaudi specific code.
        import habana_frameworks.torch.utils.experimental as htexp
        if htexp._get_device_type() == htexp.synDeviceType.synDeviceGaudi2:
            assert self.q_config.q_range_dtype == torch.float8_e4m3fnuz, "Gaudi2 has a unique dtype: float8_e4m3fnuz"

        # Reshape the tensor
        val_reshaped = val.view(self.num_groups, group_size).float()
        # Calculate the scale
        max_vals = val_reshaped.abs().max(dim=1, keepdim=True)[0]
        q_range = torch.finfo(self.q_config.q_range_dtype).max
        self.scale = q_range / max_vals
        # TODO: [SW-208941] clear gaudi specific code.
        # Copy quantized
        quant, _ = torch.ops.hpu.cast_to_fp8_v2(val_reshaped,
                                                self.scale,
                                                stochastic_rounding,
                                                dtype=self.q_config.q_dtype)
        out.copy_(quant)

        return out

    def get_scales_fallback(self, out, num_groups):
        return self.scale
