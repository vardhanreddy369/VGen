"""
CPU-compatible inference wrapper for VGen
This script patches CUDA and distributed training for CPU execution
"""
import os
import sys

# Ensure we're in the right directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("Step 1: Patching CUDA...", flush=True)

import torch
import torch.distributed as dist

# Patch torch.cuda to work on CPU-only systems
class CPUCudaStub:
    """Stub to replace CUDA calls on systems without CUDA"""
    
    def __getattr__(self, name):
        if name == 'device_count':
            return lambda: 0
        elif name == 'is_available':
            return lambda: False
        elif name == 'set_device':
            return lambda x: None
        elif name == 'empty_cache':
            return lambda: None
        elif name == 'amp':
            class DummyAMP:
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return lambda: DummyAMP()
        return lambda *args, **kwargs: None

# Monkey-patch CUDA if not available
if not torch.cuda.is_available():
    print("Step 2: CUDA not available, patching...", flush=True)
    torch.cuda = CPUCudaStub()
    os.environ['CUDA_VISIBLE_DEVICES'] = ''

# Patch distributed training for CPU
print("Step 3: Patching distributed...", flush=True)
original_init_process_group = dist.init_process_group
def patched_init_process_group(*args, **kwargs):
    """Stub for distributed training that doesn't require CUDA"""
    print(f"patched_init_process_group called with backend={kwargs.get('backend')}", flush=True)
    if 'backend' in kwargs and kwargs['backend'] == 'nccl':
        kwargs['backend'] = 'gloo'  # Use gloo backend for CPU
    try:
        return original_init_process_group(*args, **kwargs)
    except RuntimeError as e:
        print(f"Warning: Could not init process group: {e}. Running in single-process mode.", flush=True)
        return None

dist.init_process_group = patched_init_process_group

# Now run the normal inference
print("Step 4: Importing config...", flush=True)
from utils.config import Config

print("Step 5: Importing registry...", flush=True)
from utils.registry_class import INFER_ENGINE

print("Step 6: Importing tools...", flush=True)
from tools import *

print("Step 7: Starting inference...", flush=True)
if __name__ == '__main__':
    try:
        print("Step 8: Loading config...", flush=True)
        cfg_update = Config(load=True)
        print("Step 9: Building inference engine...", flush=True)
        INFER_ENGINE.build(dict(type=cfg_update.TASK_TYPE), cfg_update=cfg_update.cfg_dict)
        print("Step 10: Inference complete!", flush=True)
    except Exception as e:
        print(f"Error during inference: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
