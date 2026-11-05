"""The sample campaign `evo-target init` scaffolds beside the seed tree.

`score.py` and `edit.py` are standalone programs, copied by bytes into the
demonstration directory and run there by `evo tree evolve` as the declared
scorer and `subprocess` editor; `corpus.jsonl` is the two-case corpus. They
are samples of the two program contracts, not recommended scorers or editors.
Both programs are import-safe (the product import sweeps import every module
under `evolution`): all work is in `main()` under the `__name__` guard.
"""
