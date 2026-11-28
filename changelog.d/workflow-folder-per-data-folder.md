### Changed

- **The workflow folder is now the optional `--flows-dir` parameter of `serve` and `runner`**
  (`cli.py`, `config.py`, `workflow_folder.py`). `abstractgateway serve --flows-dir <folder>`
  (and `abstractgateway runner --flows-dir <folder>`) names the folder of `.flow` workflows to
  serve. The environment variables `ABSTRACTGATEWAY_FLOWS_DIR`, `ABSTRACTFRAMEWORK_WORKFLOWS_DIR`
  and `ABSTRACTFLOW_FLOWS_DIR` are no longer read: this is a breaking change for anyone who set
  them. Pass `--flows-dir` instead (once is enough: the folder is remembered).

### Fixed

- **The gateway keeps using the same workflow folder, whatever copy of its code runs**
  (`workflow_folder.py`, `config.py`, `service.py`). An automation no longer stops with
  "Workflow '…' not found in registry" because the gateway was started from another copy of the
  code: the data folder remembers the workflow folder in `<data dir>/config/workflow-folder.json`
  and a start without `--flows-dir` uses it.
- **After an AbstractNote upgrade, automations on an older note version keep running**
  (`abstractnote_seam.py`): every AbstractNote version packed in the data folder stays loaded, the
  installed one first.
