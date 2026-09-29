# ocio

OCIO configs. The default is `-ocio ./ocio/aces_1.2cg/config.ocio`, i.e. put the ACES 1.2 config (with its `luts` folder) into `ocio/aces_1.2cg/`.

With OCIO 2.2 or newer you can skip this folder and use a built-in config, e.g. `-ocio ocio://studio-config-latest` (ACES 1.3+). Those configs are meant to be used with a display and view: `-display "sRGB - Display" -view "ACES 1.0 - SDR Video"`. `-ocio_info` lists the names a config offers.

The contents of this folder are not in git.
