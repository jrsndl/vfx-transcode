@echo off
rem ---------------------------------------------------------------------------
rem  Quick end-to-end test of vfx-transcode on the sample sequences in .\media
rem  Movies and one log per test are written to .\media\_test_output
rem  Usage: test_conversion.bat [python-executable]     (default: python)
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PY=%~1"
if "%PY%"=="" set "PY=python"
set "MEDIA=%~dp0media"
set "OUT=%MEDIA%\_test_output"
if not exist "%OUT%" mkdir "%OUT%"
set "SUMMARY=%OUT%\summary.txt"
> "%SUMMARY%" echo vfx-transcode test run %DATE% %TIME%
set FAILED=0

echo.
echo ==== 0  unit tests ====
"%PY%" -m unittest discover -s tests -t . > "%OUT%\00_unittests.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\00_unittests.log"
call :check %RC% "0  unit tests"

echo.
echo ==== 1  DB beauty: dot separator, 1001-1007, h264 default, all burn-ins ====
"%PY%" vfx-transcode.py -v 1 ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -o "%OUT%\01_db_beauty_h264.mp4" ^
  -burn_tl "{clean_name}" -burn_tr "TC {tc}" -burn_bl "{frame_start}-{frame_end}" -burn_br "{current_frame}" > "%OUT%\01_db_beauty.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\01_db_beauty.log"
call :check %RC% "1  DB beauty h264 + burn-ins"

echo.
echo ==== 2  LS_AMB beauty: underscore separator, starts at 1000, ProRes 422 HQ, 16-bit, 25 fps, review burn-ins minus bottom-left ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\LS_AMB_SH101_BEAUTY_v005\LS_AMB_SH101_BEAUTY_v005_1002.exr" ^
  -o "%OUT%\02_ls_amb_prores422hq.mov" ^
  -preset prores422hq -mezzanine_16bit 1 -r 25 ^
  -burn_preset review -burn_bl "" > "%OUT%\02_ls_amb.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\02_ls_amb.log"
call :check %RC% "2  LS_AMB beauty prores422hq"

echo.
echo ==== 3  eqs_sh020: frames from 0000, h264-web, 1280 wide, sRGB output, header JSON, dailies burn-ins ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\eqs_sh020__v01\eqs_sh020__v01.0000.exr" ^
  -o "%OUT%\03_eqs_h264web_srgb.mp4" ^
  -preset h264-web -width 1280 -co out_srgb -write_exr_info 1 ^
  -burn_preset dailies > "%OUT%\03_eqs.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\03_eqs.log"
call :check %RC% "3  eqs_sh020 h264-web srgb"

echo.
echo ==== 4  DB utilities: automatic channel pick, one oiiotool per file, temp next to the source ====
"%PY%" vfx-transcode.py -v 1 ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_utils\DB_000_0030_lgt_explosion_v47_utilities.1004.exr" ^
  -o "%OUT%\04_db_utils_perfile.mp4" ^
  -oiio_per_file 1 -write_exr_info 1 -temp_location source > "%OUT%\04_db_utils.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\04_db_utils.log"
call :check %RC% "4  DB utilities per-file"

echo.
echo ==== 5  DB deep EXR as depth; frame rate from the Karma header wins over -r 25 ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_deep\DB_000_0030_lgt_explosion_v47.1001.exr" ^
  -o "%OUT%\05_db_deep.mp4" -r 25 -fps_source exr > "%OUT%\05_db_deep.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\05_db_deep.log"
call :check %RC% "5  DB deep depth + fps from header"

echo.
echo ==== 6  timecode from argument + metadata + reel name, ProRes mov; movie metadata dumped into the log ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -o "%OUT%\06_db_beauty_tc_meta.mov" -preset prores422hq ^
  -timecode 01:00:00:00 -burn_tr "TC {tc}" ^
  -metadata "title={clean_name}" -metadata "comment=review, start TC {timecode}" ^
  -metadata_copy "all,-husk:*" -reelid "{clean_name}" > "%OUT%\06_tc_meta.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\06_db_beauty_tc_meta.mov" >> "%OUT%\06_tc_meta.log" 2>&1
type "%OUT%\06_tc_meta.log"
call :check %RC% "6  timecode arg + metadata + reel mov"

echo.
echo ==== 7  timecode from EXR header, falls back to the frame counter if there is none ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\LS_AMB_SH101_BEAUTY_v005\LS_AMB_SH101_BEAUTY_v005_1000.exr" ^
  -o "%OUT%\07_ls_amb_tc_exr.mp4" -timecode_source exr -burn_tr "TC {tc}" ^
  -metadata_copy all > "%OUT%\07_tc_exr.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\07_ls_amb_tc_exr.mp4" >> "%OUT%\07_tc_exr.log" 2>&1
type "%OUT%\07_tc_exr.log"
call :check %RC% "7  timecode from EXR header mp4"

rem ---------------------------------------------------------------------------
rem  8-13: frame rate, timecode and reel name on ICOI2 (header: owner, timeCode,
rem  framesPerSecond 24). Each test also checks its log and the movie itself
rem  (ffmpeg's dump in NN_*_probe.txt); a missing line is reported as [FAIL].
rem ---------------------------------------------------------------------------
echo.
echo ==== 8  ICOI2 mov: frame rate, timecode and reel name all from the EXR header ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -burn_preset review ^
  -fps_source exr -timecode_source exr -reelid_source exr ^
  -o "%OUT%\08_icoi_header.mov" > "%OUT%\08_icoi_header.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\08_icoi_header.mov" > "%OUT%\08_icoi_header_probe.txt" 2>&1
type "%OUT%\08_icoi_header.log"
call :check %RC% "8  icoi_header"
call :expect "%OUT%\08_icoi_header.log" "Frame rate: 24 (EXR header FramesPerSecond)" "8  icoi_header"
call :expect "%OUT%\08_icoi_header.log" "Timecode: 00:00:41:17 (EXR header)" "8  icoi_header"
call :expect "%OUT%\08_icoi_header.log" "Reel name: Z017_C001_20190920_R (EXR header owner)" "8  icoi_header"
call :expect "%OUT%\08_icoi_header_probe.txt" "reel_name" "8  icoi_header movie"
call :expect "%OUT%\08_icoi_header_probe.txt" "Z017_C001_20190920_R" "8  icoi_header movie"
call :expect "%OUT%\08_icoi_header_probe.txt" "00:00:41:17" "8  icoi_header movie"
call :expect "%OUT%\08_icoi_header_probe.txt" "24 fps" "8  icoi_header movie"

echo.
echo ==== 9  ICOI2 mp4 from frame 1004: -r 25 wins over the header; header TC moved back to 1001; reel in mp4 only as a tag ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1004.exr" ^
  -r 25 -timecode_source exr ^
  -o "%OUT%\09_icoi_r25_tc_shift.mp4" > "%OUT%\09_icoi_r25_tc_shift.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\09_icoi_r25_tc_shift.mp4" > "%OUT%\09_icoi_r25_tc_shift_probe.txt" 2>&1
type "%OUT%\09_icoi_r25_tc_shift.log"
call :check %RC% "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift.log" "Frame rate: 25 (-r)" "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift.log" "EXR header 24 ignored" "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift.log" "Timecode: 00:00:41:17 (EXR header)" "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift.log" "Reel name: Z017_C001_20190920_R (EXR header owner)" "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift.log" "keeps it only as a reel_name metadata tag" "9  icoi_r25_tc_shift"
call :expect "%OUT%\09_icoi_r25_tc_shift_probe.txt" "25 fps" "9  icoi_r25_tc_shift movie"
call :expect "%OUT%\09_icoi_r25_tc_shift_probe.txt" "00:00:41:17" "9  icoi_r25_tc_shift movie"
call :expect "%OUT%\09_icoi_r25_tc_shift_probe.txt" "Z017_C001_20190920_R" "9  icoi_r25_tc_shift movie"

echo.
echo ==== 10  ICOI2: -r 25 but -fps_source exr, header 24 wins; counter timecode; no reel name ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -r 25 -fps_source exr -reelid_source off ^
  -o "%OUT%\10_icoi_fps_exr_wins.mp4" > "%OUT%\10_icoi_fps_exr_wins.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\10_icoi_fps_exr_wins.mp4" > "%OUT%\10_icoi_fps_exr_wins_probe.txt" 2>&1
type "%OUT%\10_icoi_fps_exr_wins.log"
call :check %RC% "10 icoi_fps_exr_wins"
call :expect "%OUT%\10_icoi_fps_exr_wins.log" "Frame rate: 24 (EXR header FramesPerSecond)" "10 icoi_fps_exr_wins"
call :expect "%OUT%\10_icoi_fps_exr_wins.log" "-r 25 ignored" "10 icoi_fps_exr_wins"
call :expect "%OUT%\10_icoi_fps_exr_wins.log" "Timecode: 00:00:41:17 (frame counter)" "10 icoi_fps_exr_wins"
call :expect_not "%OUT%\10_icoi_fps_exr_wins.log" "Reel name:" "10 icoi_fps_exr_wins"
call :expect "%OUT%\10_icoi_fps_exr_wins_probe.txt" "24 fps" "10 icoi_fps_exr_wins movie"

echo.
echo ==== 11  ICOI2: -fps_source arg_only -r 25, header not read; counter timecode at 25; explicit reel name ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -r 25 -fps_source arg_only ^
  -reelid_source arg -reelid "{clean_name}" ^
  -o "%OUT%\11_icoi_arg_only.mov" > "%OUT%\11_icoi_arg_only.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\11_icoi_arg_only.mov" > "%OUT%\11_icoi_arg_only_probe.txt" 2>&1
type "%OUT%\11_icoi_arg_only.log"
call :check %RC% "11 icoi_arg_only"
call :expect "%OUT%\11_icoi_arg_only.log" "Frame rate: 25 (-r)" "11 icoi_arg_only"
call :expect "%OUT%\11_icoi_arg_only.log" "Timecode: 00:00:40:01 (frame counter)" "11 icoi_arg_only"
call :expect "%OUT%\11_icoi_arg_only.log" "Reel name: ICOI2_005_033_renderCompSecondary_v000 (-reelid)" "11 icoi_arg_only"
call :expect_not "%OUT%\11_icoi_arg_only.log" "ignored" "11 icoi_arg_only"
call :expect "%OUT%\11_icoi_arg_only_probe.txt" "25 fps" "11 icoi_arg_only movie"
call :expect "%OUT%\11_icoi_arg_only_probe.txt" "00:00:40:01" "11 icoi_arg_only movie"
call :expect "%OUT%\11_icoi_arg_only_probe.txt" "ICOI2_005_033_renderCompSecondary_v000" "11 icoi_arg_only movie"

echo.
echo ==== 12  ICOI2 mxf DNxHR: -r 23.976 snapped to 24000/1001; reel name cut by -reelid_pattern ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset dnxhr-sq -ff_out_ext mxf -r 23.976 ^
  -reelid_source exr -reelid_pattern "^(Z[0-9]+_C[0-9]+)" ^
  -o "%OUT%\12_icoi_23976_dnxhr.mxf" > "%OUT%\12_icoi_23976_dnxhr.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\12_icoi_23976_dnxhr.mxf" > "%OUT%\12_icoi_23976_dnxhr_probe.txt" 2>&1
type "%OUT%\12_icoi_23976_dnxhr.log"
call :check %RC% "12 icoi_23976_dnxhr"
call :expect "%OUT%\12_icoi_23976_dnxhr.log" "Frame rate: 23.976 (-r)" "12 icoi_23976_dnxhr"
call :expect "%OUT%\12_icoi_23976_dnxhr.log" "Reel name: Z017_C001 (EXR header owner)" "12 icoi_23976_dnxhr"
call :expect "%OUT%\12_icoi_23976_dnxhr_probe.txt" "23.98 fps" "12 icoi_23976_dnxhr movie"
call :expect "%OUT%\12_icoi_23976_dnxhr_probe.txt" "reel_name" "12 icoi_23976_dnxhr movie"
call :expect "%OUT%\12_icoi_23976_dnxhr_probe.txt" "Z017_C001" "12 icoi_23976_dnxhr movie"

echo.
echo ==== 13  ICOI2 mov: reel name from an arbitrary attribute (nuke/node_hash); camera metadata copied ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq ^
  -reelid_source exr -reelid_attr nuke/node_hash -metadata_copy "all,-nuke/*,-MEDIA_PATH" ^
  -o "%OUT%\13_icoi_attr_reel_copy.mov" > "%OUT%\13_icoi_attr_reel_copy.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\13_icoi_attr_reel_copy.mov" > "%OUT%\13_icoi_attr_reel_copy_probe.txt" 2>&1
type "%OUT%\13_icoi_attr_reel_copy.log"
call :check %RC% "13 icoi_attr_reel_copy"
call :expect "%OUT%\13_icoi_attr_reel_copy.log" "Reel name: db920c72b1fecf8c (EXR header nuke/node_hash)" "13 icoi_attr_reel_copy"
call :expect_not "%OUT%\13_icoi_attr_reel_copy.log" "MEDIA_PATH" "13 icoi_attr_reel_copy"
call :expect "%OUT%\13_icoi_attr_reel_copy_probe.txt" "db920c72b1fecf8c" "13 icoi_attr_reel_copy movie"
call :expect "%OUT%\13_icoi_attr_reel_copy_probe.txt" "DJI Zemuse X7" "13 icoi_attr_reel_copy movie"

echo.
echo ==== 14  ProRes preset vendor_id=appl; reports which vendor your ffmpeg writes on the video track ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -reelid_source off ^
  -o "%OUT%\14_prores_vendor.mov" > "%OUT%\14_prores_vendor.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\14_prores_vendor.mov" > "%OUT%\14_prores_vendor_probe.txt" 2>&1
type "%OUT%\14_prores_vendor.log"
findstr /L /C:"vendor_id" "%OUT%\14_prores_vendor_probe.txt"
call :check %RC% "14 prores_vendor"
call :expect "%OUT%\14_prores_vendor_probe.txt" "vendor_id       : appl" "14 prores_vendor movie"
findstr /L /C:"vendor_id       : FFMP" "%OUT%\14_prores_vendor_probe.txt" >nul 2>&1 && >> "%SUMMARY%" echo [INFO] 14 prores_vendor - video track vendor is still FFMP: this ffmpeg's mov writer does not take vendor_id

rem ---------------------------------------------------------------------------
rem  15-18: metadata presets (presets.json "metadata_presets") on ICOI2
rem ---------------------------------------------------------------------------

echo.
echo ==== 15  ICOI2 mov: metadata preset review; explicit literal reel name beats the EXR owner ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -metadata_preset review -reelid A001C003 ^
  -o "%OUT%\15_icoi_review_reel_literal.mov" > "%OUT%\15_icoi_review_reel_literal.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\15_icoi_review_reel_literal.mov" > "%OUT%\15_icoi_review_reel_literal_probe.txt" 2>&1
type "%OUT%\15_icoi_review_reel_literal.log"
call :check %RC% "15 icoi_review_reel_literal"
call :expect "%OUT%\15_icoi_review_reel_literal.log" "Reel name: A001C003 (-reelid)" "15 icoi_review_reel_literal"
call :expect "%OUT%\15_icoi_review_reel_literal.log" "(preset review)" "15 icoi_review_reel_literal"
call :expect "%OUT%\15_icoi_review_reel_literal_probe.txt" "reel_name" "15 icoi_review_reel_literal movie"
call :expect "%OUT%\15_icoi_review_reel_literal_probe.txt" "A001C003" "15 icoi_review_reel_literal movie"
call :expect "%OUT%\15_icoi_review_reel_literal_probe.txt" "ICOI2_005_033_renderCompSecondary_v000 1001-1010 @ 24 fps" "15 icoi_review_reel_literal movie"
call :expect "%OUT%\15_icoi_review_reel_literal_probe.txt" "vendor_id       : appl" "15 icoi_review_reel_literal movie"
call :expect_not "%OUT%\15_icoi_review_reel_literal_probe.txt" "Z017_C001" "15 icoi_review_reel_literal movie"

echo.
echo ==== 16  ICOI2 mov from frame 1004: metadata preset conform, TC and reel name from the header ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1004.exr" ^
  -preset prores422hq -metadata_preset conform ^
  -o "%OUT%\16_icoi_conform.mov" > "%OUT%\16_icoi_conform.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\16_icoi_conform.mov" > "%OUT%\16_icoi_conform_probe.txt" 2>&1
type "%OUT%\16_icoi_conform.log"
call :check %RC% "16 icoi_conform"
call :expect "%OUT%\16_icoi_conform.log" "Timecode: 00:00:41:17 (EXR header)" "16 icoi_conform"
call :expect "%OUT%\16_icoi_conform.log" "Reel name: Z017_C001_20190920_R (EXR header owner)" "16 icoi_conform"
call :expect "%OUT%\16_icoi_conform.log" "(preset conform)" "16 icoi_conform"
call :expect "%OUT%\16_icoi_conform_probe.txt" "reel_name" "16 icoi_conform movie"
call :expect "%OUT%\16_icoi_conform_probe.txt" "Z017_C001_20190920_R" "16 icoi_conform movie"
call :expect "%OUT%\16_icoi_conform_probe.txt" "00:00:41:17" "16 icoi_conform movie"

echo.
echo ==== 17  ICOI2 mov: metadata preset archive copies the camera attributes, not nuke/* or MEDIA_PATH ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -metadata_preset archive ^
  -o "%OUT%\17_icoi_archive.mov" > "%OUT%\17_icoi_archive.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\17_icoi_archive.mov" > "%OUT%\17_icoi_archive_probe.txt" 2>&1
type "%OUT%\17_icoi_archive.log"
call :check %RC% "17 icoi_archive"
call :expect "%OUT%\17_icoi_archive.log" "(preset archive)" "17 icoi_archive"
call :expect_not "%OUT%\17_icoi_archive.log" "MEDIA_PATH" "17 icoi_archive"
call :expect "%OUT%\17_icoi_archive_probe.txt" "DJI Zemuse X7" "17 icoi_archive movie"
call :expect "%OUT%\17_icoi_archive_probe.txt" "transcoded" "17 icoi_archive movie"
call :expect "%OUT%\17_icoi_archive_probe.txt" "preset prores422hq" "17 icoi_archive movie"
call :expect_not "%OUT%\17_icoi_archive_probe.txt" "db920c72b1fecf8c" "17 icoi_archive movie"

echo.
echo ==== 18  ICOI2 mp4: metadata preset client, title only, no reel name, nothing from the header ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -metadata_preset client ^
  -o "%OUT%\18_icoi_client.mp4" > "%OUT%\18_icoi_client.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\18_icoi_client.mp4" > "%OUT%\18_icoi_client_probe.txt" 2>&1
type "%OUT%\18_icoi_client.log"
call :check %RC% "18 icoi_client"
call :expect "%OUT%\18_icoi_client.log" "(preset client)" "18 icoi_client"
call :expect_not "%OUT%\18_icoi_client.log" "Reel name:" "18 icoi_client"
call :expect "%OUT%\18_icoi_client_probe.txt" "ICOI2_005_033_renderCompSecondary_v000" "18 icoi_client movie"
call :expect_not "%OUT%\18_icoi_client_probe.txt" "Z017_C001" "18 icoi_client movie"
call :expect_not "%OUT%\18_icoi_client_probe.txt" "DJI" "18 icoi_client movie"

echo.
echo ==== 19  burn-in places and styles: preset wip, a styled bottom centre from the command line ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_preset wip -burn_bc "{clean_name}" -burn_opacity 0.9 -burn_style "bc:color=#80ff80,size=40,box=1" ^
  -o "%OUT%\19_burnin_styles.mp4" > "%OUT%\19_burnin_styles.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -ss 0 -i "%OUT%\19_burnin_styles.mp4" -frames:v 1 "%OUT%\19_burnin_styles.png" >nul 2>&1
type "%OUT%\19_burnin_styles.log"
call :check %RC% "19 burnin_styles"
call :expect "%OUT%\19_burnin_styles.log" "tc 'WIP - NOT FOR APPROVAL'  [color=yellow, opacity=0.9, size=2.2" "19 burnin_styles"
call :expect "%OUT%\19_burnin_styles.log" "bc '{clean_name}'  [color=#80ff80, opacity=0.9, size=40, box=True]" "19 burnin_styles"
call :expect "%OUT%\19_burnin_styles.log" "tl '{clean_name}'  [color=white, opacity=0.9, size=1.33" "19 burnin_styles"
if exist "%OUT%\19_burnin_styles.png" (>> "%SUMMARY%" echo [INFO] 19 burnin_styles - first frame saved as 19_burnin_styles.png, check it by eye)

echo.
echo ==== 20  {meta:ATTR}: header values of the FIRST frame in burn-ins and metadata, -i on frame 1004 ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1004.exr" ^
  -burn_tl "{meta:owner}" -burn_tc "{meta:cameraModel|no camera}" -burn_tr "{meta:nuke/input/timecode}" ^
  -burn_bl "{meta:no_such_attribute|fallback text}" -burn_br "{current_frame}" -burn_box 1 ^
  -metadata "source_hash={meta:nuke/node_hash}" -reelid_source off ^
  -o "%OUT%\20_meta_keywords.mp4" > "%OUT%\20_meta_keywords.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\20_meta_keywords.mp4" > "%OUT%\20_meta_keywords_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\20_meta_keywords.mp4" -frames:v 1 "%OUT%\20_meta_keywords.png" >nul 2>&1
type "%OUT%\20_meta_keywords.log"
call :check %RC% "20 meta_keywords"
call :expect "%OUT%\20_meta_keywords.log" "Header value {meta:owner} = 'Z017_C001_20190920_R[00000-02773]'" "20 meta_keywords"
call :expect "%OUT%\20_meta_keywords.log" "Header value {meta:nuke/input/timecode} = '00:00:41:17'" "20 meta_keywords"
call :expect "%OUT%\20_meta_keywords.log" "Header value {meta:no_such_attribute} not found" "20 meta_keywords"
call :expect "%OUT%\20_meta_keywords_probe.txt" "db920c72b1fecf8c" "20 meta_keywords movie"
if exist "%OUT%\20_meta_keywords.png" (>> "%SUMMARY%" echo [INFO] 20 meta_keywords - first frame saved as 20_meta_keywords.png, check the burn-ins by eye)

echo.
echo ==== 21  -ocio_info: names in the default ACES 1.2 config and the built-in ACES 1.3 studio config ====
"%PY%" vfx-transcode.py -ocio_info > "%OUT%\21_ocio_info.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py -ocio_info -ocio ocio://studio-config-latest >> "%OUT%\21_ocio_info.log" 2>&1
type "%OUT%\21_ocio_info.log"
call :check %RC% "21 ocio_info"

echo.
echo ==== 22  display/view: ACES 1.3 studio config, sRGB - Display / ACES 1.0 - SDR Video ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -ocio ocio://studio-config-latest -ci ACEScg -display "sRGB - Display" -view "ACES 1.0 - SDR Video" ^
  -burn_preset review ^
  -o "%OUT%\22_display_view.mp4" > "%OUT%\22_display_view.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\22_display_view.mp4" > "%OUT%\22_display_view_probe.txt" 2>&1
type "%OUT%\22_display_view.log"
call :check %RC% "22 display_view"
call :expect "%OUT%\22_display_view.log" "Colour: ACEScg" "22 display_view"
call :expect "%OUT%\22_display_view.log" "view " "22 display_view"
call :expect "%OUT%\22_display_view_probe.txt" "iec61966-2-1" "22 display_view movie"

echo.
echo ==== 23  pixel aspect: -par 2 overrides the header's 1, output keeps the pixels and is tagged 2:1 ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -par 2 -par_out same ^
  -o "%OUT%\23_par_same.mp4" > "%OUT%\23_par_same.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\23_par_same.mp4" > "%OUT%\23_par_same_probe.txt" 2>&1
type "%OUT%\23_par_same.log"
call :check %RC% "23 par_same"
call :expect "%OUT%\23_par_same.log" "Pixel aspect: 2 (-par)" "23 par_same"
call :expect "%OUT%\23_par_same.log" "EXR header 1 ignored" "23 par_same"
call :expect "%OUT%\23_par_same_probe.txt" "SAR 2:1" "23 par_same movie"

echo.
echo ==== 24  pixel aspect: -par 2 reformatted to square pixels, height halved ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -par 2 -par_out square ^
  -o "%OUT%\24_par_square.mp4" > "%OUT%\24_par_square.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\24_par_square.mp4" > "%OUT%\24_par_square_probe.txt" 2>&1
type "%OUT%\24_par_square.log"
call :check %RC% "24 par_square"
call :expect "%OUT%\24_par_square.log" "Pixel aspect: 2 (-par)" "24 par_square"
call :expect "%OUT%\24_par_square.log" "output 1, height x0.5" "24 par_square"
call :expect "%OUT%\24_par_square_probe.txt" "SAR 1:1" "24 par_square movie"

echo.
echo ==== 25  pixel aspect: -par 2 but -par_source exr, so the header's 1 wins ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -par 2 -par_source exr ^
  -o "%OUT%\25_par_exr_wins.mp4" > "%OUT%\25_par_exr_wins.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\25_par_exr_wins.mp4" > "%OUT%\25_par_exr_wins_probe.txt" 2>&1
type "%OUT%\25_par_exr_wins.log"
call :check %RC% "25 par_exr_wins"
call :expect "%OUT%\25_par_exr_wins.log" "-par 2 ignored" "25 par_exr_wins"
call :expect "%OUT%\25_par_exr_wins_probe.txt" "SAR 1:1" "25 par_exr_wins movie"

echo.
echo ==== 26  multi-line burn-ins with \n in all six places ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_size 2.5%% -burn_tl "{clean_name}\n{meta:owner}" -burn_tc "CENTRE\nsecond, longer line" ^
  -burn_tr "right\nTC {tc}" -burn_bl "bottom\nleft\nthree" -burn_bc "shot\n{current_frame} / {frame_end}" ^
  -burn_br "{fps} fps\n{date}" -burn_style "bc:box=1" ^
  -o "%OUT%\26_multiline.mp4" > "%OUT%\26_multiline.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\26_multiline.mp4" -frames:v 1 "%OUT%\26_multiline.png" >nul 2>&1
type "%OUT%\26_multiline.log"
call :check %RC% "26 multiline"
if exist "%OUT%\26_multiline.png" (>> "%SUMMARY%" echo [INFO] 26 multiline - first frame saved as 26_multiline.png, check the line layout by eye)

echo.
echo ===========================================================================
type "%SUMMARY%"
echo.
echo Failed tests: %FAILED%     Movies and logs: %OUT%
echo ===========================================================================
pause
exit /b %FAILED%

rem --- arg 1 = exit code, arg 2 = test name. No parentheses in names. ---
:check
if "%~1"=="0" (
  >> "%SUMMARY%" echo [ OK ] %~2
) else if "%~1"=="2" (
  >> "%SUMMARY%" echo [WARN] %~2 - movie written, some frames failed and were held
) else (
  >> "%SUMMARY%" echo [FAIL] %~2 - exit code %~1
  set /a FAILED+=1
)
exit /b 0

rem --- arg 1 = file, arg 2 = text that must appear, arg 3 = test name -------------
:expect
findstr /L /C:"%~2" "%~1" >nul 2>&1 && exit /b 0
>> "%SUMMARY%" echo [FAIL] %~3 - missing: %~2
set /a FAILED+=1
exit /b 0

rem --- arg 1 = file, arg 2 = text that must NOT appear, arg 3 = test name ---------
:expect_not
findstr /L /C:"%~2" "%~1" >nul 2>&1 || exit /b 0
>> "%SUMMARY%" echo [FAIL] %~3 - unexpected: %~2
set /a FAILED+=1
exit /b 0
