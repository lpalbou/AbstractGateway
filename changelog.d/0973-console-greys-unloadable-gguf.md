### Fixed

- **A GGUF this computer's llama.cpp cannot load is never offered for download or as the default** (backlog 0973,
  AbstractCore 0867/0868). The catalog card shows a greyed **Download** or **Use as default**, with the reason as
  its tooltip ("This computer's llama.cpp (llama-cpp-python 0.3.16) cannot load the spark2_5 architecture. Update
  llama-cpp-python, or choose another model."). The same applies to a downloaded model outside the catalog. The flag
  comes from the catalog artifact (the Hub's architecture) or from the downloaded row.
