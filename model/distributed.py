import torch
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors
import torch.distributed as dist
from torch.nn.modules import Module


class DistributedDataParallel(Module):

    def __init__(self, module):
        super(DistributedDataParallel, self).__init__()
        self.warn_on_half = True if dist._backend == dist.dist_backend.GLOO else False

        self.module = module

        for p in self.module.state_dict().values():
            if torch.is_tensor(p):
                dist.broadcast(p, 0)

        def allreduce_params():
            if(self.needs_reduction):
                self.needs_reduction = False
                buckets = {}
                for name, param in self.module.named_parameters():
                    if param.requires_grad and param.grad is not None:
                        tp = type(param.data)
                        if tp not in buckets:
                            buckets[tp] = []
                        buckets[tp].append(param)
                if self.warn_on_half:
                    if torch.cuda.HalfTensor in buckets:
                        print("WARNING: gloo dist backend for half parameters may be extremely slow." +
                              " It is recommended to use the NCCL backend in this case.")
                        self.warn_on_half = False

                for tp in buckets:
                    bucket = buckets[tp]
                    grads = [param.grad.data for param in bucket]
                    coalesced = _flatten_dense_tensors(grads)
                    dist.all_reduce(coalesced)
                    torch.cuda.synchronize()
                    coalesced /= dist.get_world_size()
                    for buf, synced in zip(grads, _unflatten_dense_tensors(coalesced, grads)):
                        buf.copy_(synced)
        self.hook_handles = []
        self.hooks = []
        for param in list(self.module.parameters()):
            def allreduce_hook(*unused):
                param._execution_engine.queue_callback(allreduce_params)
            handle = param.register_hook(allreduce_hook)
            self.hooks.append(allreduce_hook)
            self.hook_handles.append(handle)

    def forward(self, *inputs, **kwargs):
        self.needs_reduction = True
        return self.module(*inputs, **kwargs)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        [h.remove() for h in self.hook_handles]
        sd = self.module.state_dict(destination, prefix, keep_vars)
        for handle, hook in zip(self.hook_handles, self.hooks):
            d = handle.hooks_dict_ref()
            d[handle.id] = hook

        return sd

    def load_state_dict(self, state_dict, strict=True):
        self.module.load_state_dict(state_dict, strict=strict)

    '''
    def _sync_buffers(self):
        buffers = list(self.module._all_buffers())
        if len(buffers) > 0:
            # cross-node buffer sync
            flat_buffers = _flatten_dense_tensors(buffers)
            dist.broadcast(flat_buffers, 0)
            for buf, synced in zip(buffers, _unflatten_dense_tensors(flat_buffers, buffers)):
                buf.copy_(synced)
    def train(self, mode=True):
        # Clear NCCL communicator and CUDA event cache of the default group ID,
        # These cache will be recreated at the later call. This is currently a
        # work-around for a potential NCCL deadlock.
        if dist._backend == dist.dist_backend.NCCL:
            dist._clear_group_cache()
        super(DistributedDataParallel, self).train(mode)
        self.module.train(mode)
    '''

