# jl-uboot-tool (vendored subset)

From https://github.com/kagaimiq/jl-uboot-tool at commit
`adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a`, MIT licensed (see `LICENSE`). All files are unchanged.

- `scsiio/`: the kernel SCSI pass-through used on Windows (`\\.\PhysicalDriveN`) and Linux (`/dev/sgN`).
- `data/loaderblobs/usb/bd19loader.bin` (Gamebrick, SHA256 `faa1b519…df4f`) and
  `br23loader.bin` (Ananke, SHA256 `7384429e…6ddf`): the RAM loaders uploaded in update mode.
  Each flasher pins its loader's hash and refuses any other.
