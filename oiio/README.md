# oiio

Put `oiiotool.exe` here together with the DLLs it needs; the default is `-oiio ./oiio/oiiotool.exe`. If it's missing, `oiiotool` on the PATH is used.

Tested with OpenImageIO 3.0. It needs OCIO support (for `--colorconvert`) and OpenEXR, including deep data. Any OIIO build works: vcpkg, a studio build, or the command-line tools that come with the OpenImageIO Python package.

The contents of this folder are not in git.
