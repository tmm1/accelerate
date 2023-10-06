#!/usr/bin/env python

# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import importlib
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

import psutil
import torch

from accelerate.commands.config import default_config_file, load_config_from_file
from accelerate.commands.config.config_args import SageMakerConfig
from accelerate.state import get_int_from_env
from accelerate.utils import (
    Arguments,
    ComputeEnvironment,
    DistributedType,
    PrepareForLaunch,
    _filter_args,
    is_bf16_available,
    is_deepspeed_available,
    is_npu_available,
    is_rich_available,
    is_sagemaker_available,
    is_torch_version,
    is_tpu_available,
    is_xpu_available,
    patch_environment,
    prepare_deepspeed_cmd_env,
    prepare_multi_gpu_env,
    prepare_sagemager_args_inputs,
    prepare_simple_launcher_cmd_env,
    prepare_tpu,
)
from accelerate.utils.constants import DEEPSPEED_MULTINODE_LAUNCHERS


if is_rich_available():
    from rich import get_console
    from rich.logging import RichHandler

    FORMAT = "%(message)s"
    logging.basicConfig(format=FORMAT, datefmt="[%X]", handlers=[RichHandler()])


logger = logging.getLogger(__name__)

options_to_group = {
    "--multi-gpu": "Distributed GPUs",
    "--tpu": "TPU",
    "--use_deepspeed": "DeepSpeed Arguments",
    "--use_fsdp": "FSDP Arguments",
    "--use_megatron_lm": "Megatron-LM Arguments",
}


def clean_option(option):
    "Finds all cases of - after the first two characters and changes them to _"
    if option.startswith("--"):
        return option[:3] + option[3:].replace("-", "_")


class _CustomHelpAction(argparse._HelpAction):
    """
    This is a custom help action that will hide all arguments that are not used in the command line when the help is
    called. This is useful for the case where the user is using a specific platform and only wants to see the arguments
    for that platform.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if "accelerate" in sys.argv[0] and "launch" in sys.argv[1:]:
            args = sys.argv[2:]
        else:
            args = sys.argv[1:]
        opts = parser._actions
        titles = [
            "Hardware Selection Arguments",
            "Resource Selection Arguments",
            "Training Paradigm Arguments",
            "positional arguments",
            "optional arguments",
        ]
        if len(args) > 1:
            used_platforms = [arg for arg in args if arg in options_to_group.keys()]
            args = list(map(clean_option, args))
            used_titles = [options_to_group[o] for o in used_platforms]
            for i, arg in enumerate(opts):
                # If the argument's container is outside of the used titles, hide it
                if arg.container.title not in titles + used_titles:
                    setattr(opts[i], "help", argparse.SUPPRESS)
                # If the argument is hardware selection, but not being passed, hide it
                elif arg.container.title == "Hardware Selection Arguments":
                    if set(arg.option_strings).isdisjoint(set(args)):
                        setattr(opts[i], "help", argparse.SUPPRESS)
                    else:
                        setattr(opts[i], "help", arg.help + " (currently selected)")
                # If the argument is a training paradigm, but not being passed, hide it
                elif arg.container.title == "Training Paradigm Arguments":
                    if set(arg.option_strings).isdisjoint(set(used_platforms)):
                        setattr(opts[i], "help", argparse.SUPPRESS)
                    else:
                        setattr(opts[i], "help", arg.help + " (currently selected)")
            for i, group in enumerate(list(parser._action_groups)):
                # If all arguments in the group are hidden, hide the group
                if all([arg.help == argparse.SUPPRESS for arg in group._group_actions]):
                    parser._action_groups.remove(group)

        super().__call__(parser, namespace, values, option_string)


@dataclass
class ResourceArguments(Arguments):
    """
    Arguments for fine-tuning what and how available hardware should be used.

    Args:
        cpu (`bool`, *optional*, defaults to `False`):
            Whether or not to force the training on the CPU.
        multi_gpu (`bool`, *optional*, defaults to `False`):
            Whether or not this should launch a distributed GPU training.
        tpu (`bool`, *optional*, defaults to `False`):
            Whether or not this should launch a TPU training.
        ipex (`bool`, *optional*, defaults to `False`):
            Whether or not this should launch a Intel PyTorch Extension (IPEX) training.
        mixed_precision (`str`, *optional*, defaults to `no`):
            Whether or not to use mixed precision training. Choose between FP16, BF16 (bfloat16) or FP8 training. BF16
            training is only supported on Nvidia Ampere GPUs and PyTorch 1.10 or later.
        num_processes (`int`, *optional*, defaults to `None`):
            The total number of processes to be launched in parallel.
        num_machines (`int`, *optional*, defaults to `None`):
            The total number of machines used in this training.
        num_cpu_threads_per_process (`int`, *optional*, defaults to `None`):
            The number of CPU threads per process. Can be tuned for optimal performance.
        use_deepspeed (`bool`, *optional*, defaults to `False`):
            Whether to use deepspeed.
        use_fsdp (`bool`, *optional*, defaults to `False`):
            Whether to use fsdp.
        use_megatron_lm (`bool`, *optional*, defaults to `False`):
            Whether to use Megatron-LM.
        use_xpu (`bool`, *optional*, defaults to `False`):
            Whether to use IPEX plugin to speed up training on XPU specifically.
    """

    cpu: bool = False
    multi_gpu: bool = False
    tpu: bool = False
    ipex: bool = False
    mixed_precision: Literal["no", "fp16", "bf16", "fp8"] = "no"
    num_processes: int = None
    num_machines: int = None
    num_cpu_threads_per_process: int = None
    use_deepspeed: bool = False
    use_fsdp: bool = False
    use_megatron_lm: bool = False
    use_xpu: bool = False


@dataclass
class DynamoArguments(Arguments):
    """
    Arguments related to `torchdynamo`

    Args:
        backend (`str`):
            Backend to optimize your training with dynamo, see more at https://github.com/pytorch/torchdynamo.
        mode (`str`, *optional*, defaults to "default"):
            Mode to optimize your training with dynamo.
        use_fullgraph (`bool`, *optional*):
            Whether to use full graph mode for dynamo or it is ok to break model into several subgraphs.
        use_dynamic (`bool`, *optional*):
            Whether to enable dynamic shape tracing.
    """

    prefix: ClassVar[str] = "dynamo_"
    backend: Literal[
        "no",
        "eager",
        "aot_eager",
        "inductor",
        "nvfuser",
        "aot_nvfuser",
        "aot_cudagraphs",
        "ofi",
        "fx2trt",
        "onnxrt",
        "ipex",
    ] = "no"
    mode: Literal["default", "reduce-overhead", "max-autotune"] = "default"
    use_fullgraph: bool = False
    use_dynamic: bool = False


@dataclass
class CUDAArguments(Arguments):
    """
    Arguments related to CUDA usage.

    Args:
        gpu_ids (`str`):
            What GPUs (by id) should be used for training on this machine as a comma-seperated list.
        same_network (`bool`):
            Whether all machines used for multinode training exist on the same local network.
        machine_rank (`int`):
            The rank of the machine on which this script is launched.
        main_process_ip (`str`):
            The IP address of the machine of rank 0.
        main_process_port (`int`):
            The port to use to communicate with the machine of rank 0.
        tee (`str`, *optional*, defaults to "0"):
            Tee std streams into a log file and also to console.
        role (`str`, *optional*, defaults to "default"):
            User-defined role for the workers.
        rdzv_backend (`str`, *optional*, defaults to "static"):
            The rendezvous method to use, such as "static" or "c10d".
        rdzv_conf (`str`, *optional*, defaults to ""):
            Additional rendezvous configuration (<key1>=<value1>,<key2>=<value2>,...).
        max_restarts (`int`, *optional*, defaults to 0):
            Maximum number of worker group restarts before failing.
        monitor_interval (`float`, *optional*, defaults to 5.0):
            Interval, in seconds, to monitor the state of workers.
    """

    gpu_ids: str = None
    same_network: bool = False
    machine_rank: int = None
    main_process_ip: str = None
    main_process_port: int = None
    tee: str = "0"
    role: str = "default"
    rdzv_backend: Literal["static", "c10d"] = "static"
    rdzv_conf: str = ""
    max_restarts: int = 0
    monitor_interval: float = 5.0


@dataclass
class TPUArguments(Arguments):
    """
    Arguments related to TPU usage.

    Args:
        tpu_cluster (`bool`):
            Whether to use a GCP TPU pod for training.
        tpu_use_sudo (`bool`):
            Whether to use `sudo` when running the TPU training script in each pod.
        vm (list of `str`):
            List of single Compute VM instance names. If not provided we assume usage of instance groups. For TPU pods.
        env (list of `str`):
            List of environment variables to set on the Compute VM instances. For TPU pods.
        main_training_function (`str`):
            The name of the main function to be executed in your script (only for TPU training).
        downcast_bf16 (`bool`):
            Whether when using bf16 precision on TPUs if both float and double tensors are cast to bfloat16 or if
            double tensors remain as float32.
    """

    tpu_cluster: bool = False
    tpu_use_sudo: bool = False
    vm: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    main_training_function: str = None
    downcast_bf16: bool = False


@dataclass
class DeepSpeedArguments(Arguments):
    """
    Arguments related to DeepSpeed

    Args:
        deepspeed_config_file (`str`, *optional*):
            DeepSpeed config file to use.
        zero_stage (`int`, *optional*, defaults to 2):
            DeepSpeed's ZeRO optimization stage.
        offload_optimizer_device (`str`, *optional*, defaults to "none"):
            Decides where (none|cpu|nvme) to offload optimizer states.
        offload_param_device (`str`, *optional*, defaults to "none"):
            Decides where (none|cpu|nvme) to offload parameters.
        offload_optimizer_nvme_path (`str`, *optional*, defaults to "none"):
            Decides Nvme Path to offload optimizer states.
        offload_param_nvme_path (`str`, *optional*, defaults to "none"):
            Decides Nvme Path to offload parameters.
        gradient_accumulation_steps (`int`, *optional*, defaults to 1):
            Number of gradient_accumulation_steps used in your training script when using deepspeed.
        gradient_clipping (`float`, *optional*, defaults to 1.0):
            Gradient clipping value used in your training script when using deepspeed.
        zero3_init_flag (`bool`, *optional*):
            Whether to enable `deepspeed.zero.Init` for constructing massive models. Only applicable with DeepSpeed
            ZeRO Stage-3.
        zero3_save_16bit_model (`bool`, *optional*):
            Whether to save 16-bit model weights when using ZeRO Stage-3. Only applicable with DeepSpeed ZeRO Stage-3.
        deepspeed_hostfile (`str`, *optional*):
            DeepSpeed hostfile for configuring multi-node compute resources.
        deepspeed_exclusion_filter (`str`, *optional*):
            DeepSpeed exclusion filter string when using mutli-node setup.
        deepspeed_inclusion_filter (`str`, *optional*):
            DeepSpeed inclusion filter string when using mutli-node setup.
        deepspeed_multinode_launcher (`str`, *optional*, defaults to "pdsh"):
            DeepSpeed multi-node launcher to use.
    """

    config_file: str = None
    zero_stage: int = 2
    offload_optimizer_device: Literal["none", "cpu", "nvme"] = "none"
    offload_param_device: Literal["none", "cpu", "nvme"] = "none"
    offload_optimizer_nvme_path: str = "none"
    offload_param_nvme_path: str = "none"
    gradient_accumulation_steps: int = 1
    gradient_clipping: float = 1.0
    zero3_init_flag: bool = True
    zero3_save_16bit_model: bool = False
    deepspeed_hostfile: str = None
    deepspeed_exclusion_filter: str = None
    deepspeed_inclusion_filter: str = None
    deepspeed_multinode_launcher: Literal["pdsh", "standard", "openmpi", "mvapich", "mpich"] = "pdsh"


@dataclass
class FSDPArguments(Arguments):
    """
    Arguments related to Fully Shared Data Parallelism (FSDP)

    Args:
        offload_params (`bool`, *optional*):
            Decides whether to offload parameters and gradients to CPU.
        min_num_params (`int`, *optional*, defaults to 1e8):
            FSDP's minimum number of parameters for Default Auto Wrapping.
        sharding_strategy (`int`, *optional*, defaults to 1):
            FSDP's Sharding Strategy.
        auto_wrap_policy (`str`, *optional*):
            FSDP's auto wrap policy.
        transformer_layer_cls_to_wrap (`str`, *optional*):
            Transformer layer class name (case-sensitive) to wrap ,e.g, `BertLayer`, `GPTJBlock`, `T5Block` ....
        backward_prefetch_policy (`str`, *optional*):
            FSDP's backward prefetch policy.
        state_dict_type (`str`, *optional*):
            FSDP's state dict type.
        forward_prefetch (`bool`, *optional*):
            Whether to explicitly prefetch the next upcoming all-gather while executing in the forward pass.
        use_orig_params (`bool`, *optional*):
            Whether to allow non-uniform `requires_grad` during init, which means support for interspersed frozen and
            trainable parameters.
        sync_module_states (`bool`, *optional*, defaults to `True`):
            Whether to broadcast module parameters from rank 0.
    """

    prefix: ClassVar[str] = "fsdp_"
    offload_params: bool = False
    min_num_params: int = 1e8
    sharding_strategy: int = 1
    auto_wrap_policy: str = None
    transformer_layer_cls_to_wrap: str = None
    backward_prefetch_policy: str = None
    state_dict_type: str = None
    forward_prefetch: bool = False
    use_orig_params: bool = False
    sync_module_states: bool = True


@dataclass
class MegatronLMArguments(Arguments):
    """
    Arguments related to MegaTron-LM

    Args:
        tp_degree (`int`, *optional*, defaults to 1):
            Tensor Parallelism (TP) degree.
        pp_degree (`int`, *optional*, defaults to 1):
            Pipeline Parallelism (PP) degree.
        num_micro_batches (`int`, *optional*):
            Number of micro batches when `pp_degree` > 1.
        sequence_parallelism (`bool`, *optional*):
            Whether to enable Sequence Parallelism when `tp_degree` > 1.
        recompute_activations (`bool`, *optional*):
            Whether to enable Selective Activation Recomputation.
        use_distributed_optimizer (`bool`, *optional*):
            Whether to use distributed optimizer which shards optimizer state and gradients across Data Pralellel (DP)
            ranks.
        gradient_clipping (`float`, *optional*, defaults to 1.0):
            Gradient clipping value based on global L2 Norm (0 to disable).
    """

    prefix: ClassVar[str] = "megatron_lm_"
    tp_degree: int = 1
    pp_degree: int = 1
    num_micro_batches: int = None
    sequence_parallelism: bool = None
    recompute_activations: bool = None
    use_distributed_optimizer: bool = None
    gradient_clipping: float = 1.0


@dataclass
class AWSArguments(Arguments):
    """
    Arguments related to AWS

    Args:
        access_key_id (`str`, *optional*):
            The AWS_ACCESS_KEY_ID used to launch the Amazon SageMaker training job.
        secret_access_key (`str`, *optional*):
            The AWS_SECRET_ACCESS_KEY used to launch the Amazon SageMaker training job.
    """

    prefix: ClassVar[str] = "aws_"
    access_key_id: str = None
    secret_access_key: str = None


def launch_command_parser(subparsers=None):
    if subparsers is not None:
        parser = subparsers.add_parser("launch", add_help=False, allow_abbrev=False)
    else:
        parser = argparse.ArgumentParser("Accelerate launch command", add_help=False, allow_abbrev=False)

    parser.register("action", "help", _CustomHelpAction)
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit.")

    parser.add_argument(
        "--config_file",
        type=str,
        default=None,
        help="The config file to use for the default values in the launching script.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Silence subprocess errors from the launch stack trace and only show the relevant tracebacks. (Only applicable to DeepSpeed and single-process configurations)",
    )
    parser.add_argument(
        "-m",
        "--module",
        action="store_true",
        help="Change each process to interpret the launch script as a Python module, executing with the same behavior as 'python -m'.",
    )
    parser.add_argument(
        "--no_python",
        action="store_true",
        help="Skip prepending the training script with 'python' - just execute it directly. Useful when the script is not a Python script.",
    )

    # Resource selection arguments
    resource_args = parser.add_argument_group(
        "Resource Selection Arguments", "Arguments for fine-tuning how available hardware should be used."
    )
    ResourceArguments().add_to_parser(resource_args)

    # Dynamo arguments
    DynamoArguments().add_to_parser(resource_args)

    # distributed GPU training arguments
    distributed_args = parser.add_argument_group("Distributed GPUs", "Arguments related to distributed GPU training.")
    CUDAArguments().add_to_parser(distributed_args)

    # TPU arguments
    tpu_args = parser.add_argument_group("TPU", "Arguments related to TPU.")
    TPUArguments().add_to_parser(tpu_args)
    tpu_args.add_argument(
        "--no_tpu_cluster",
        action="store_false",
        dest="tpu_use_cluster",
        help="Should not be passed explicitly, this is for internal use only.",
    )
    # DeepSpeed arguments
    deepspeed_args = parser.add_argument_group("DeepSpeed Arguments", "Arguments related to DeepSpeed.")
    DeepSpeedArguments().add_to_parser(deepspeed_args)

    # fsdp arguments
    fsdp_args = parser.add_argument_group("FSDP Arguments", "Arguments related to Fully Shared Data Parallelism.")
    FSDPArguments().add_to_parser(fsdp_args)

    # megatron_lm args
    megatron_lm_args = parser.add_argument_group("Megatron-LM Arguments", "Arguments related to Megatron-LM.")
    MegatronLMArguments().add_to_parser(megatron_lm_args)

    # AWS arguments
    aws_args = parser.add_argument_group("AWS Arguments", "Arguments related to AWS.")
    AWSArguments().add_to_parser(aws_args)

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Whether to print out the torch.distributed stack trace when something fails.",
    )
    parser.add_argument(
        "training_script",
        type=str,
        help=(
            "The full path to the script to be launched in parallel, followed by all the arguments for the training "
            "script."
        ),
    )

    # Other arguments of the training scripts
    parser.add_argument("training_script_args", nargs=argparse.REMAINDER, help="Arguments of the training script.")

    if subparsers is not None:
        parser.set_defaults(func=launch_command)
    return parser


def simple_launcher(args):
    cmd, current_env = prepare_simple_launcher_cmd_env(args)

    process = subprocess.Popen(cmd, env=current_env)
    process.wait()
    if process.returncode != 0:
        if not args.quiet:
            raise subprocess.CalledProcessError(returncode=process.returncode, cmd=cmd)
        else:
            sys.exit(1)


def multi_gpu_launcher(args):
    import torch.distributed.run as distrib_run

    current_env = prepare_multi_gpu_env(args)

    debug = getattr(args, "debug", False)
    args = _filter_args(
        args,
        distrib_run.get_args_parser(),
        ["--training_script", args.training_script, "--training_script_args", args.training_script_args],
    )
    with patch_environment(**current_env):
        try:
            distrib_run.run(args)
        except Exception:
            if is_rich_available() and debug:
                console = get_console()
                console.print("\n[bold red]Using --debug, `torch.distributed` Stack Trace:[/bold red]")
                console.print_exception(suppress=[__file__], show_locals=False)
            else:
                raise


def deepspeed_launcher(args):
    import torch.distributed.run as distrib_run

    if not is_deepspeed_available():
        raise ImportError("DeepSpeed is not installed => run `pip3 install deepspeed` or build it from source.")

    cmd, current_env = prepare_deepspeed_cmd_env(args)

    if args.num_machines > 1 and args.deepspeed_multinode_launcher != DEEPSPEED_MULTINODE_LAUNCHERS[1]:
        with open(".deepspeed_env", "a") as f:
            for key, value in current_env.items():
                if ";" in value or " " in value:
                    continue
                f.write(f"{key}={value}\n")

        process = subprocess.Popen(cmd, env=current_env)
        process.wait()
        if process.returncode != 0:
            if not args.quiet:
                raise subprocess.CalledProcessError(returncode=process.returncode, cmd=cmd)
            else:
                sys.exit(1)
    else:
        debug = getattr(args, "debug", False)
        args = _filter_args(
            args,
            distrib_run.get_args_parser(),
            ["--training_script", args.training_script, "--training_script_args", args.training_script_args],
        )
        with patch_environment(**current_env):
            try:
                distrib_run.run(args)
            except Exception:
                if is_rich_available() and debug:
                    console = get_console()
                    console.print("\n[bold red]Using --debug, `torch.distributed` Stack Trace:[/bold red]")
                    console.print_exception(suppress=[__file__], show_locals=False)
                else:
                    raise


def tpu_launcher(args):
    import torch_xla.distributed.xla_multiprocessing as xmp

    if args.no_python:
        raise ValueError("--no_python cannot be used with TPU launcher")

    args, current_env = prepare_tpu(args, {})

    if args.module:
        mod_name = args.training_script
    else:
        # Import training_script as a module
        script_path = Path(args.training_script)
        sys.path.append(str(script_path.parent.resolve()))
        mod_name = script_path.stem

    mod = importlib.import_module(mod_name)
    if not hasattr(mod, args.main_training_function):
        raise ValueError(
            f"Your training script should have a function named {args.main_training_function}, or you should pass a "
            "different value to `--main_training_function`."
        )

    # Patch sys.argv
    sys.argv = [mod.__file__] + args.training_script_args

    main_function = getattr(mod, args.main_training_function)
    with patch_environment(**current_env):
        xmp.spawn(PrepareForLaunch(main_function), args=(), nprocs=args.num_processes)


def tpu_pod_launcher(args):
    from torch_xla.distributed import xla_dist

    current_env = {}
    args, current_env = prepare_tpu(args, current_env, True)
    debug = getattr(args, "debug", False)

    training_script = args.training_script
    training_script_args = args.training_script_args
    new_args = _filter_args(
        args, xla_dist.get_args_parser(), ["--tpu", args.tpu_name, "--positional", "", "--restart-tpuvm-pod-server"]
    )

    if args.tpu_use_sudo:
        new_cmd = ["sudo"]
    else:
        new_cmd = []

    new_cmd += [
        "accelerate-launch",
        "--tpu",
        "--no_tpu_cluster",
        "--num_machines",
        str(1),
        "--mixed_precision",
        "no",
        "--dynamo_backend",
        "no",
        "--num_processes",
        str(args.num_processes),
        "--main_training_function",
        str(args.main_training_function),
        training_script,
    ] + training_script_args

    new_args.positional = new_cmd
    bad_flags = ""
    for arg in vars(new_args):
        if arg.startswith("docker_"):
            value = getattr(new_args, arg)
            if value != "" and value is not None:
                bad_flags += f'{arg}="{value}"\n'
    if bad_flags != "":
        raise ValueError(
            f"Docker containers are not supported for TPU pod launcher currently, please remove the following flags:\n{bad_flags}"
        )
    new_args.env = [f"{k}={v}" for k, v in current_env.items()]
    new_args.env.append("ACCELERATE_IN_TPU_POD=1")
    try:
        xla_dist.resolve_and_execute(new_args)
    except Exception:
        if is_rich_available() and debug:
            console = get_console()
            console.print("\n[bold red]Using --debug, `torch_xla.xla_dist` Stack Trace:[/bold red]")
            console.print_exception(suppress=[__file__], show_locals=False)
        else:
            raise


def sagemaker_launcher(sagemaker_config: SageMakerConfig, args):
    if not is_sagemaker_available():
        raise ImportError(
            "Please install sagemaker to be able to launch training on Amazon SageMaker with `pip install accelerate[sagemaker]`"
        )
    if args.module or args.no_python:
        raise ValueError(
            "SageMaker requires a python training script file and cannot be used with --module or --no_python"
        )

    from sagemaker.huggingface import HuggingFace

    args, sagemaker_inputs = prepare_sagemager_args_inputs(sagemaker_config, args)

    huggingface_estimator = HuggingFace(**args)

    huggingface_estimator.fit(inputs=sagemaker_inputs)
    print(f"You can find your model data at: {huggingface_estimator.model_data}")


def _validate_launch_command(args):
    # Sanity checks
    if sum([args.multi_gpu, args.cpu, args.tpu, args.use_deepspeed, args.use_fsdp]) > 1:
        raise ValueError(
            "You can only use one of `--cpu`, `--multi_gpu`, `--tpu`, `--use_deepspeed`, `--use_fsdp` at a time."
        )
    if args.multi_gpu and (args.num_processes is not None) and (args.num_processes < 2):
        raise ValueError("You need to use at least 2 processes to use `--multi_gpu`.")

    defaults = None
    warned = []
    mp_from_config_flag = False
    # Get the default from the config file.
    if args.config_file is not None or os.path.isfile(default_config_file) and not args.cpu:
        defaults = load_config_from_file(args.config_file)
        if (
            not args.multi_gpu
            and not args.tpu
            and not args.tpu_use_cluster
            and not args.use_deepspeed
            and not args.use_fsdp
            and not args.use_megatron_lm
        ):
            args.use_deepspeed = defaults.distributed_type == DistributedType.DEEPSPEED
            args.multi_gpu = (
                True
                if defaults.distributed_type
                in (DistributedType.MULTI_GPU, DistributedType.MULTI_NPU, DistributedType.MULTI_XPU)
                else False
            )
            args.tpu = defaults.distributed_type == DistributedType.TPU
            args.use_fsdp = defaults.distributed_type == DistributedType.FSDP
            args.use_megatron_lm = defaults.distributed_type == DistributedType.MEGATRON_LM
            args.tpu_use_cluster = defaults.tpu_use_cluster if args.tpu else False
        if args.gpu_ids is None:
            if defaults.gpu_ids is not None:
                args.gpu_ids = defaults.gpu_ids
            else:
                args.gpu_ids = "all"

        if args.multi_gpu and args.num_machines is None:
            args.num_machines = defaults.num_machines

        if len(args.gpu_ids.split(",")) < 2 and (args.gpu_ids != "all") and args.multi_gpu and args.num_machines <= 1:
            raise ValueError(
                "Less than two GPU ids were configured and tried to run on on multiple GPUs. "
                "Please ensure at least two are specified for `--gpu_ids`, or use `--gpu_ids='all'`."
            )
        if defaults.compute_environment == ComputeEnvironment.LOCAL_MACHINE:
            # Update args with the defaults
            for name, attr in defaults.__dict__.items():
                if isinstance(attr, dict):
                    for k in defaults.deepspeed_config:
                        setattr(args, k, defaults.deepspeed_config[k])
                    for k in defaults.fsdp_config:
                        arg_to_set = k
                        if "fsdp" not in arg_to_set:
                            arg_to_set = "fsdp_" + arg_to_set
                        setattr(args, arg_to_set, defaults.fsdp_config[k])
                    for k in defaults.megatron_lm_config:
                        setattr(args, k, defaults.megatron_lm_config[k])
                    for k in defaults.dynamo_config:
                        setattr(args, k, defaults.dynamo_config[k])
                    for k in defaults.ipex_config:
                        setattr(args, k, defaults.ipex_config[k])
                    continue

                # Those args are handled separately
                if (
                    name not in ["compute_environment", "mixed_precision", "distributed_type"]
                    and getattr(args, name, None) is None
                ):
                    setattr(args, name, attr)
        if not args.debug:
            args.debug = defaults.debug

        if not args.mixed_precision:
            if defaults.mixed_precision is None:
                args.mixed_precision = "no"
            else:
                args.mixed_precision = defaults.mixed_precision
                mp_from_config_flag = True
        else:
            native_amp = False
            err = "{mode} mixed precision requires {requirement}"
            if args.use_cpu or (args.use_xpu and torch.xpu.is_available()):
                native_amp = is_torch_version(">=", "1.10")
            else:
                native_amp = is_bf16_available(True)
            if args.mixed_precision == "bf16" and not native_amp and not (args.tpu and is_tpu_available()):
                raise ValueError(err.format(mode="bf16", requirement="PyTorch >= 1.10 and a supported device."))

        # Silently set the default here
        if args.dynamo_backend is None:
            args.dynamo_backend = "no"
    else:
        if args.num_processes is None:
            if args.use_xpu and is_xpu_available():
                args.num_processes = torch.xpu.device_count()
            elif is_npu_available():
                args.num_processes = torch.npu.device_count()
            else:
                args.num_processes = torch.cuda.device_count()
            warned.append(f"\t`--num_processes` was set to a value of `{args.num_processes}`")
        if args.debug is None:
            args.debug = False
        if not args.multi_gpu and (
            (args.use_xpu and is_xpu_available() and torch.xpu.device_count() > 1)
            or (is_npu_available() and torch.npu.device_count() > 1)
            or (torch.cuda.device_count() > 1)
        ):
            warned.append(
                "\t\tMore than one GPU was found, enabling multi-GPU training.\n"
                "\t\tIf this was unintended please pass in `--num_processes=1`."
            )
            args.multi_gpu = True
        if args.num_machines is None:
            warned.append("\t`--num_machines` was set to a value of `1`")
            args.num_machines = 1
        if args.mixed_precision is None:
            warned.append("\t`--mixed_precision` was set to a value of `'no'`")
            args.mixed_precision = "no"
        if not hasattr(args, "use_cpu"):
            args.use_cpu = args.cpu
        if args.dynamo_backend is None:
            warned.append("\t`--dynamo_backend` was set to a value of `'no'`")
            args.dynamo_backend = "no"
    if args.debug:
        logger.debug("Running script in debug mode, expect distributed operations to be slightly slower.")

    is_aws_env_disabled = defaults is None or (
        defaults is not None and defaults.compute_environment != ComputeEnvironment.AMAZON_SAGEMAKER
    )
    if is_aws_env_disabled and args.num_cpu_threads_per_process is None:
        args.num_cpu_threads_per_process = 1
        if args.use_cpu and args.num_processes >= 1:
            local_size = get_int_from_env(
                ["MPI_LOCALNRANKS", "OMPI_COMM_WORLD_LOCAL_SIZE", "MV2_COMM_WORLD_LOCAL_SIZE"], 1
            )
            threads_per_process = int(psutil.cpu_count(logical=False) / local_size)
            if threads_per_process > 1:
                args.num_cpu_threads_per_process = threads_per_process
                warned.append(
                    f"\t`--num_cpu_threads_per_process` was set to `{args.num_cpu_threads_per_process}` to improve out-of-box performance when training on CPUs"
                )

    if any(warned):
        message = "The following values were not passed to `accelerate launch` and had defaults used instead:\n"
        message += "\n".join(warned)
        message += (
            "\nTo avoid this warning pass in values for each of the problematic parameters or run `accelerate config`."
        )
        logger.warning(message)
    return args, defaults, mp_from_config_flag


def launch_command(args):
    args, defaults, mp_from_config_flag = _validate_launch_command(args)
    # Use the proper launcher
    if args.use_deepspeed and not args.cpu:
        args.deepspeed_fields_from_accelerate_config = list(defaults.deepspeed_config.keys()) if defaults else []
        if mp_from_config_flag:
            args.deepspeed_fields_from_accelerate_config.append("mixed_precision")
        args.deepspeed_fields_from_accelerate_config = ",".join(args.deepspeed_fields_from_accelerate_config)
        deepspeed_launcher(args)
    elif args.use_fsdp and not args.cpu:
        multi_gpu_launcher(args)
    elif args.use_megatron_lm and not args.cpu:
        multi_gpu_launcher(args)
    elif args.multi_gpu and not args.cpu:
        multi_gpu_launcher(args)
    elif args.tpu and not args.cpu:
        if args.tpu_use_cluster:
            tpu_pod_launcher(args)
        else:
            tpu_launcher(args)
    elif defaults is not None and defaults.compute_environment == ComputeEnvironment.AMAZON_SAGEMAKER:
        sagemaker_launcher(defaults, args)
    else:
        simple_launcher(args)


def main():
    parser = launch_command_parser()
    args = parser.parse_args()
    launch_command(args)


if __name__ == "__main__":
    main()
