# jl-misctools (vendored subset)

From https://github.com/kagaimiq/jl-misctools at commit
`0a5b12db0ef38f3042acffbe2452730a37fd2405`, MIT licensed (see `LICENSE`).

Only the runtime pieces the Gamebrick and Ananke flashers use are kept:

- `firmware/jltech/cipher.py`, `firmware/jltech/chipkeybin.py`: unchanged.
- `firmware/jltech/crc.py`: rewritten to use `binascii.crc_hqx`, which computes the same
  CRC-16 (polynomial 0x1021, non-reflected, caller-supplied initial value) as upstream's
  `crcmod` function, so the flasher needs no compiled dependency. `jl_crc32` was dropped (unused).
- `firmware/jltech/__init__.py`: added (empty) so `jltech` is a regular package for PyInstaller.
