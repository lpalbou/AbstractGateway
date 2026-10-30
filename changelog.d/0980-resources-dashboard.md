- Resources now leads with CPU, RAM, GPU activity and data-volume capacity, real
  browser-local activity charts, topology-aware memory capacity and contextual
  inspectors. Unified accelerator heap is explicitly overlapping RAM; dedicated
  CUDA VRAM uses device-wide total/free counters, separately from process usage.
- The existing AbstractUIC themes, top-right docs/About/appearance/account widgets
  and resource mutation handlers remain in place. Explanations use kit info
  tooltips; Gateway management and memory details use kit modal binding.
- Host state adds independently sampled CPU, storage, swap, disk and network
  observations, with warm-up/reset/unsupported states instead of guessed zeros.
  Sampling is shared, read-only and in-process; no new service or dependency is
  installed. History is bounded to one hour/720 samples in the signed-in page.
