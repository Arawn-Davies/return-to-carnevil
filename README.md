# carnevil-recomp

Static recompilation of the 1998 Midway Seattle arcade game CarnEvil, to run natively.
This repository holds only tooling and notes.

## No game data

Game data (disk images, extracted files, ROMs, code listings) is never committed. Tools and
tests read it from `$CARNEVIL_WORK` (default `/Volumes/archive/reincarnevilation/work`).
Tests that need it skip when it is missing.

## Usage

Python 3, standard library only.

```
python3 -m carnevil_tools split carnevil.raw -o parts
python3 -m carnevil_tools extract parts/part0 -o part0_files
python3 -m unittest discover -s tests -v
```

## Credits

The disk format was learned from [xclusivor/CarnEvil-Research](https://github.com/xclusivor/CarnEvil-Research),
itself based on daemon1's tools from the XenTaX forums. See `docs/analysis/disk-format.md`.
