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
call :expect "%OUT%\08_icoi_header.log" "Frame rate: 24 (EXR header" "8  icoi_header"
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
call :expect "%OUT%\10_icoi_fps_exr_wins.log" "Frame rate: 24 (EXR header" "10 icoi_fps_exr_wins"
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
echo ==== 27  slate: generic preset with a note; slate is frame 1000 at TC 00:00:41:16, pictures keep 00:00:41:17 ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -slate generic -slate_field "Notes=first comp pass, check the helmet edge against the sky" ^
  -burn_preset review ^
  -o "%OUT%\27_slate_generic.mov" > "%OUT%\27_slate_generic.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\27_slate_generic.mov" > "%OUT%\27_slate_generic_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\27_slate_generic.mov" -vf "select=lt(n\,2),scale=960:-2,tile=1x2" -frames:v 1 "%OUT%\27_slate_generic.png" >nul 2>&1
type "%OUT%\27_slate_generic.log"
call :check %RC% "27 slate_generic"
call :expect "%OUT%\27_slate_generic.log" "Timecode: 00:00:41:17 (frame counter), slate 00:00:41:16" "27 slate_generic"
call :expect "%OUT%\27_slate_generic.log" "Slate (preset generic): frame 1000" "27 slate_generic"
call :expect "%OUT%\27_slate_generic_probe.txt" "00:00:41:16" "27 slate_generic movie"
if exist "%OUT%\27_slate_generic.png" (>> "%SUMMARY%" echo [INFO] 27 slate_generic - slate and first picture saved as 27_slate_generic.png, check by eye)

echo.
echo ==== 28  slate at timecode 00:00:00:00: the slate keeps it, the pictures start at 00:00:00:01 ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preset prores422hq -timecode 00:00:00:00 -slate editorial -burn_preset editorial ^
  -o "%OUT%\28_slate_tc_zero.mov" > "%OUT%\28_slate_tc_zero.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\28_slate_tc_zero.mov" > "%OUT%\28_slate_tc_zero_probe.txt" 2>&1
type "%OUT%\28_slate_tc_zero.log"
call :check %RC% "28 slate_tc_zero"
call :expect "%OUT%\28_slate_tc_zero.log" "pictures start at 00:00:00:01" "28 slate_tc_zero"
call :expect "%OUT%\28_slate_tc_zero_probe.txt" "00:00:00:00" "28 slate_tc_zero movie"

echo.
echo ==== 29  slate on anamorphic 2.0: frames squeezed to half width, -par 2 un-squeezes them; slate matches ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -oiio_command "--resize 960x962" -par 2 -par_out same -slate generic -slate_thumbnail first ^
  -o "%OUT%\29_slate_anamorphic.mp4" > "%OUT%\29_slate_anamorphic.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\29_slate_anamorphic.mp4" > "%OUT%\29_slate_anamorphic_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\29_slate_anamorphic.mp4" -vf "select=eq(n\,0),scale=iw*sar:ih,setsar=1,scale=1920:-2" -frames:v 1 "%OUT%\29_slate_anamorphic.png" >nul 2>&1
type "%OUT%\29_slate_anamorphic.log"
call :check %RC% "29 slate_anamorphic"
call :expect "%OUT%\29_slate_anamorphic.log" "thumbnail of frame 1001" "29 slate_anamorphic"
call :expect "%OUT%\29_slate_anamorphic.log" "960x962" "29 slate_anamorphic squeezed"
call :expect "%OUT%\29_slate_anamorphic_probe.txt" "SAR 2:1" "29 slate_anamorphic movie"
if exist "%OUT%\29_slate_anamorphic.png" (>> "%SUMMARY%" echo [INFO] 29 slate_anamorphic - un-squeezed slate saved as 29_slate_anamorphic.png, text and thumbnail should look normal, the movie too)

echo.
echo ==== 30  plain slate from -slate_field only, underscore sequence from 1000, 16-bit, burn-ins on the slate too ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\LS_AMB_SH101_BEAUTY_v005\LS_AMB_SH101_BEAUTY_v005_1000.exr" ^
  -mezzanine_16bit 1 -slate_field "Shot={clean_name}" -slate_field "Artist={meta:Artist|unknown}" ^
  -slate_field "Frames={frame_start} - {frame_end}" -slate_burnin 1 -burn_preset frame ^
  -o "%OUT%\30_slate_plain.mp4" > "%OUT%\30_slate_plain.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\30_slate_plain.mp4" -frames:v 1 "%OUT%\30_slate_plain.png" >nul 2>&1
type "%OUT%\30_slate_plain.log"
call :check %RC% "30 slate_plain"
call :expect "%OUT%\30_slate_plain.log" "Slate: frame 999" "30 slate_plain"
call :expect "%OUT%\30_slate_plain.log" "rgb48be" "30 slate_plain 16-bit"
if exist "%OUT%\30_slate_plain.png" (>> "%SUMMARY%" echo [INFO] 30 slate_plain - slate saved as 30_slate_plain.png, frame counter 999 in the corner)

echo.
echo ==== 31  slate style: half size, top aligned, own sizes and colours ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -slate generic -slate_field "Notes=style test" -slate_scale 50%% -slate_valign top ^
  -slate_title_size 6%% -slate_subtitle_size 3%% -slate_size 3%% ^
  -slate_label_color #555555 -slate_value_color #ffcc00 -slate_rule 1 -slate_style "title_color=#88ccff,line_color=#ffcc00" ^
  -o "%OUT%\31_slate_style.mp4" > "%OUT%\31_slate_style.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\31_slate_style.mp4" -frames:v 1 "%OUT%\31_slate_style.png" >nul 2>&1
type "%OUT%\31_slate_style.log"
call :check %RC% "31 slate_style"
if exist "%OUT%\31_slate_style.png" (>> "%SUMMARY%" echo [INFO] 31 slate_style - slate saved as 31_slate_style.png: half size at the top, yellow values, dark labels, yellow line under the title)

echo.
echo ==== 32  letterbox 2.39 see-through with a red edge line, burn-ins on top, slate without bars ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -slate generic -burn_preset review ^
  -letterbox 1 -letterbox_ratio 2.39 -letterbox_color 0,0,0,160 -letterbox_line 3 -letterbox_line_color 255,0,0,255 ^
  -o "%OUT%\32_letterbox.mp4" > "%OUT%\32_letterbox.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\32_letterbox.mp4" -vf "select=lt(n\,2),scale=960:-2,tile=1x2" -frames:v 1 "%OUT%\32_letterbox.png" >nul 2>&1
type "%OUT%\32_letterbox.log"
call :check %RC% "32 letterbox"
call :expect "%OUT%\32_letterbox.log" "Letterbox 2.39: letterbox" "32 letterbox"
if exist "%OUT%\32_letterbox.png" (>> "%SUMMARY%" echo [INFO] 32 letterbox - slate and first picture in 32_letterbox.png: clean slate, dark see-through bars with a red edge)

echo.
echo ==== 33  pillarbox: 4:3 on the 16:9 picture, solid bars ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_preset review -letterbox 1 -letterbox_ratio 4:3 ^
  -o "%OUT%\33_pillarbox.mp4" > "%OUT%\33_pillarbox.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\33_pillarbox.mp4" -frames:v 1 "%OUT%\33_pillarbox.png" >nul 2>&1
type "%OUT%\33_pillarbox.log"
call :check %RC% "33 pillarbox"
call :expect "%OUT%\33_pillarbox.log" "Letterbox 4:3: pillarbox" "33 pillarbox"

echo.
echo ==== 34  name keywords: version and name splits in burn-ins, metadata title, slate and reel name ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_tl "{namesplit2}_{namesplit3}" -burn_tr "v{version_string} / {namesplit-2}" ^
  -metadata "title={namesplit2}_{namesplit3} v{version_number}" -reelid "{namesplit1}" -reelid_source arg ^
  -slate generic -slate_field "Version={version_number}" ^
  -o "%OUT%\34_name_keywords.mov" > "%OUT%\34_name_keywords.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\34_name_keywords.mov" > "%OUT%\34_name_keywords_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\34_name_keywords.mov" -vf "select=lt(n\,2),scale=960:-2,tile=1x2" -frames:v 1 "%OUT%\34_name_keywords.png" >nul 2>&1
type "%OUT%\34_name_keywords.log"
call :check %RC% "34 name_keywords"
call :expect "%OUT%\34_name_keywords.log" "Version: 000" "34 name_keywords"
call :expect "%OUT%\34_name_keywords.log" "Reel name: ICOI2" "34 name_keywords"
call :expect "%OUT%\34_name_keywords_probe.txt" "005_033 v0" "34 name_keywords movie"
if exist "%OUT%\34_name_keywords.png" (>> "%SUMMARY%" echo [INFO] 34 name_keywords - 34_name_keywords.png: slate row Version 0, burn-ins 005_033 and v000 / renderCompSecondary)

echo.
echo ==== 35  burn-in margins: 5%% all round, bottom 100 px, right 200 px, multi-line bottom left ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_preset review-boxed -burn_bl "{clean_name}\n{frame_start}-{frame_end}" ^
  -burn_margin 5%% -burn_margin_bottom 100 -burn_margin_right 200 ^
  -o "%OUT%\35_burn_margins.mp4" > "%OUT%\35_burn_margins.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\35_burn_margins.mp4" -frames:v 1 "%OUT%\35_burn_margins.png" >nul 2>&1
type "%OUT%\35_burn_margins.log"
call :check %RC% "35 burn_margins"
call :expect "%OUT%\35_burn_margins.log" "margins: top 5" "35 burn_margins"
if exist "%OUT%\35_burn_margins.png" (>> "%SUMMARY%" echo [INFO] 35 burn_margins - 35_burn_margins.png: text inset 5 percent, bottom 100 px, right 200 px)

echo.
echo ==== 36  watermark: confidential preset, plus a red two-line watermark over the letterbox ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -slate generic -burn_preset confidential ^
  -o "%OUT%\36_watermark.mp4" > "%OUT%\36_watermark.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -burn_preset review -burn_wm "{namesplit1}\nINTERNAL v{version_number}" -burn_style "wm:color=red,size=6%%,opacity=0.4" ^
  -letterbox 1 -o "%OUT%\36_watermark_red.mp4" >> "%OUT%\36_watermark.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\36_watermark.mp4" -vf "select=eq(n\,1)" -frames:v 1 "%OUT%\36_watermark.png" >nul 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\36_watermark_red.mp4" -frames:v 1 "%OUT%\36_watermark_red.png" >nul 2>&1
type "%OUT%\36_watermark.log"
call :check %RC% "36 watermark"
call :expect "%OUT%\36_watermark.log" "wm 'CONFIDENTIAL'" "36 watermark"
if exist "%OUT%\36_watermark.png" (>> "%SUMMARY%" echo [INFO] 36 watermark - 36_watermark.png and 36_watermark_red.png: centred see-through watermark)

echo.
echo ==== 37  alpha: DB beauty into ProRes 4444 with alpha, straight and premultiplied ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -preset prores4444 -alpha keep -burn_preset review ^
  -o "%OUT%\37_alpha_straight.mov" > "%OUT%\37_alpha.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -preset prores4444 -alpha keep -alpha_premult 1 ^
  -o "%OUT%\37_alpha_premult.mov" >> "%OUT%\37_alpha.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\37_alpha_straight.mov" > "%OUT%\37_alpha_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\37_alpha_straight.mov" -vf "select=eq(n\,3),alphaextract" -frames:v 1 "%OUT%\37_alpha_matte.png" >nul 2>&1
type "%OUT%\37_alpha.log"
call :check %RC% "37 alpha"
call :expect "%OUT%\37_alpha.log" "Alpha: kept" "37 alpha"
call :expect "%OUT%\37_alpha.log" "written premultiplied" "37 alpha premult"
call :expect "%OUT%\37_alpha_probe.txt" "yuva444p" "37 alpha movie"
if exist "%OUT%\37_alpha_matte.png" (>> "%SUMMARY%" echo [INFO] 37 alpha - 37_alpha_matte.png is the movie alpha of frame 1004, should match the render alpha)

echo.
echo ==== 38  oiio transform: mygreatproject_editorial on the 4312x2160 ICOI2 plate, falls to the fit-to-1080 centre-cut sub ====
"%PY%" vfx-transcode.py -v 1 ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -oiio_transform mygreatproject_editorial -width 0 -burn_preset review ^
  -o "%OUT%\38_oiio_transform.mp4" > "%OUT%\38_oiio_transform.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\38_oiio_transform.mp4" > "%OUT%\38_oiio_transform_probe.txt" 2>&1
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\38_oiio_transform.mp4" -frames:v 1 "%OUT%\38_oiio_transform.png" >nul 2>&1
type "%OUT%\38_oiio_transform.log"
call :check %RC% "38 oiio_transform"
call :expect "%OUT%\38_oiio_transform.log" "4312x2160 input" "38 oiio_transform"
call :expect "%OUT%\38_oiio_transform.log" "--resize 0x1080 --cut 1920x1080" "38 oiio_transform sub other"
call :expect "%OUT%\38_oiio_transform_probe.txt" "1920x1080" "38 oiio_transform movie"
if exist "%OUT%\38_oiio_transform.png" (>> "%SUMMARY%" echo [INFO] 38 oiio_transform - 38_oiio_transform.png: 1920x1080, the centre of the plate)

echo.
echo ==== 39  CLF: a small CLF written here, path with keywords, applied with --ociofiletransform ====
if not exist "%OUT%\luts" mkdir "%OUT%\luts"
set "CLF=%OUT%\luts\ICOI2_005.clf"
> "%CLF%" echo ^<?xml version="1.0" encoding="UTF-8"?^>
>> "%CLF%" echo ^<ProcessList id="half" compCLFversion="3"^>
>> "%CLF%" echo ^<Matrix inBitDepth="32f" outBitDepth="32f"^>^<Array dim="3 3"^>0.5 0 0 0 0.5 0 0 0 0.5^</Array^>^</Matrix^>
>> "%CLF%" echo ^</ProcessList^>
"%PY%" vfx-transcode.py -v 1 ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -clf "%OUT%\luts\{namesplit1}_{namesplit2}.clf" -slate generic ^
  -o "%OUT%\39_clf.mp4" > "%OUT%\39_clf.log" 2>&1
set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -v error -y -i "%OUT%\39_clf.mp4" -vf "select=lt(n\,2),scale=960:-2,tile=1x2" -frames:v 1 "%OUT%\39_clf.png" >nul 2>&1
type "%OUT%\39_clf.log"
call :check %RC% "39 clf"
call :expect "%OUT%\39_clf.log" "--ociofiletransform" "39 clf"
call :expect "%OUT%\39_clf.log" "ICOI2_005.clf" "39 clf path"
if exist "%OUT%\39_clf.png" (>> "%SUMMARY%" echo [INFO] 39 clf - 39_clf.png: the test CLF only halves the scene-linear values, so the picture is dark - that is expected)

echo.
echo ==== 40  output path: relative to the source, up one folder, with keywords, extension added ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "./../_test_output/40_out/{namesplit1}/{namesplit2}_{namesplit3}_v{version_number}" ^
  > "%OUT%\40_output_path.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\40_output_path.log"
call :check %RC% "40 output_path"
call :expect "%OUT%\40_output_path.log" "005_033_v0.mp4" "40 output_path"
if not exist "%OUT%\40_out\ICOI2\005_033_v0.mp4" (
  >> "%SUMMARY%" echo [FAIL] 40 output_path - movie not in _test_output\40_out\ICOI2
  set /a FAILED+=1
)

echo.
echo ==== 41  thumbnails: with the movie - busiest frame; and thumbnail only - no -o, keyword name ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\41_thumb_movie.mp4" -ot "%OUT%\41_thumb_largest" -thumbnail_preset largest1920 ^
  > "%OUT%\41_thumbnail.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -ot "./../_test_output/41_thumb_{namesplit1}_{frame}" -thumbnail_preset start512 ^
  >> "%OUT%\41_thumbnail.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
type "%OUT%\41_thumbnail.log"
call :check %RC% "41 thumbnail"
call :expect "%OUT%\41_thumbnail.log" "Thumbnail: frame" "41 thumbnail"
call :expect "%OUT%\41_thumbnail.log" "only the thumbnail" "41 thumbnail only"
if not exist "%OUT%\41_thumb_largest.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41 thumbnail - 41_thumb_largest.jpg missing
  set /a FAILED+=1
)
if not exist "%OUT%\41_thumb_DB_1001.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41 thumbnail - 41_thumb_DB_1001.jpg missing
  set /a FAILED+=1
)
if exist "%OUT%\41_thumb_largest.jpg" (>> "%SUMMARY%" echo [INFO] 41 thumbnail - 41_thumb_largest.jpg 1920 wide and 41_thumb_DB_1001.jpg 512 wide, no burn-ins)

echo.
echo ==== 41b filmstrip: with the movie - default 24x1 row; and filmstrip only - 6x4 grid, small tiles ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\41b_strip_movie.mp4" -of "%OUT%\41b_strip_row" ^
  > "%OUT%\41b_filmstrip.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\DB_000_0030_lgt_explosion_v47_beauty\DB_000_0030_lgt_explosion_v47_beauty.1001.exr" ^
  -of "./../_test_output/41b_strip_{namesplit1}_grid" -filmstrip_grid 6x4 -filmstrip_tile 320x180 ^
  >> "%OUT%\41b_filmstrip.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
type "%OUT%\41b_filmstrip.log"
call :check %RC% "41b filmstrip"
call :expect "%OUT%\41b_filmstrip.log" "24x1 grid (11520x270)" "41b filmstrip row"
call :expect "%OUT%\41b_filmstrip.log" "6x4 grid (1920x720)" "41b filmstrip grid"
call :expect "%OUT%\41b_filmstrip.log" "only the filmstrip" "41b filmstrip only"
if not exist "%OUT%\41b_strip_row.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41b filmstrip - 41b_strip_row.jpg missing
  set /a FAILED+=1
)
if not exist "%OUT%\41b_strip_DB_grid.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41b filmstrip - 41b_strip_DB_grid.jpg missing
  set /a FAILED+=1
)
if exist "%OUT%\41b_strip_row.jpg" (>> "%SUMMARY%" echo [INFO] 41b filmstrip - check 41b_strip_row.jpg 11520x270 and 41b_strip_DB_grid.jpg 1920x720: same colours as the thumbnails, black bars, no burn-ins)

echo.
echo ==== 41c filmstrip from the movie - slate and burn-ins - and from the intermediates ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\41c_movie.mp4" -slate generic -burn_preset review ^
  -of "%OUT%\41c_strip_movie" -filmstrip_preset movie6x4 ^
  > "%OUT%\41c_filmstrip.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\41c_movie2.mp4" -slate generic ^
  -of "%OUT%\41c_strip_inter" -filmstrip_source intermediates -filmstrip_tiles 12 -filmstrip_quality 75 ^
  >> "%OUT%\41c_filmstrip.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
type "%OUT%\41c_filmstrip.log"
call :check %RC% "41c filmstrip"
call :expect "%OUT%\41c_filmstrip.log" "Filmstrip: slate + 23 frames" "41c filmstrip movie with slate"
call :expect "%OUT%\41c_filmstrip.log" "from the movie" "41c filmstrip from the movie"
call :expect "%OUT%\41c_filmstrip.log" "from the intermediates" "41c filmstrip from the intermediates"
call :expect "%OUT%\41c_filmstrip.log" "jpg quality 75" "41c filmstrip quality"
call :expect_not "%OUT%\41c_filmstrip.log" "Filmstrip: slate + 11" "41c slate off by default"
if not exist "%OUT%\41c_strip_movie.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41c filmstrip - 41c_strip_movie.jpg missing
  set /a FAILED+=1
)
if not exist "%OUT%\41c_strip_inter.jpg" (
  >> "%SUMMARY%" echo [FAIL] 41c filmstrip - 41c_strip_inter.jpg missing
  set /a FAILED+=1
)
if exist "%OUT%\41c_strip_movie.jpg" (>> "%SUMMARY%" echo [INFO] 41c filmstrip - check 41c_strip_movie.jpg: slate first, burn-ins on the frames; 41c_strip_inter.jpg: same colours as 41b, no slate)

echo.
echo ==== 42  preview: one frame as the movie shows it, plus slate and JSON; the tuner uses this ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -preview "%OUT%\42_preview.png" -thumbnail_frame 1007 -burn_preset review -slate generic -letterbox 1 ^
  -oiio_transform_args "--resize 1920x0" > "%OUT%\42_preview.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\42_preview.log"
call :check %RC% "42 preview"
call :expect "%OUT%\42_preview.json" "renderCompSecondary_v000.1007.exr" "42 preview json"
if not exist "%OUT%\42_preview_slate.png" (
  >> "%SUMMARY%" echo [FAIL] 42 preview - 42_preview_slate.png missing
  set /a FAILED+=1
)
if exist "%OUT%\42_preview.png" (>> "%SUMMARY%" echo [INFO] 42 preview - 42_preview.png shows frame 1007 with burn-ins and letterbox, TC 00:00:41:23)

echo.
echo ==== 43  name tokens from presets.json and -name_token, in -o and a burn-in; delivery metadata tags ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\43_{shot}_{pass}.mov" -name_token "pass=render([A-Za-z]+)_v" ^
  -burn_preset none -burn_tl "{shot} {pass}" -slate generic -metadata_preset delivery > "%OUT%\43_name_tokens.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\43_name_tokens.log"
call :check %RC% "43 name tokens"
call :expect "%OUT%\43_name_tokens.log" "Name token {shot} = 'ICOI2_005_033'" "43 name tokens shot"
call :expect "%OUT%\43_name_tokens.log" "Name token {pass} = 'CompSecondary'" "43 name tokens pass"
call :expect "%OUT%\43_name_tokens.log" "slate_length, display_type, watermarking" "43 delivery tags"
if not exist "%OUT%\43_ICOI2_005_033_CompSecondary.mov" (
  >> "%SUMMARY%" echo [FAIL] 43 name tokens - 43_ICOI2_005_033_CompSecondary.mov missing
  set /a FAILED+=1
)

echo.
echo ==== 44  smart tokens preset example_project: project, shot label, task, version in -o and a burn-in ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\44_{project}_{task}_{version}.mov" -smart_tokens example_project ^
  -burn_preset none -burn_bl "{shot_label} {task} {version}" > "%OUT%\44_smart_tokens.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\44_smart_tokens.log"
call :check %RC% "44 smart tokens"
call :expect "%OUT%\44_smart_tokens.log" "Smart token {shot_label} = 'ICOI2 005 033'" "44 smart tokens shot_label"
call :expect "%OUT%\44_smart_tokens.log" "Smart token {task} = 'comp'" "44 smart tokens task"
if not exist "%OUT%\44_ICOI2_comp_v000.mov" (
  >> "%SUMMARY%" echo [FAIL] 44 smart tokens - 44_ICOI2_comp_v000.mov missing
  set /a FAILED+=1
)

echo.
echo ==== 45  movie input: the mov of test 44 as input - slate, burn-ins, preview of one frame, a super-preset ====
> "%OUT%\45_super_presets.json" echo {"super_presets": {"t45": {"notes": "test", "areas": {"letterbox": {"state": {}, "args": ["-letterbox", "1", "-letterbox_ratio", "2.39"]}}}}}
"%PY%" vfx-transcode.py -i "%OUT%\44_ICOI2_comp_v000.mov" -o "%OUT%\45_from_movie.mp4" ^
  -slate generic -burn_preset none -burn_tl "{clean_name} {current_frame} {tc}" ^
  -super_presets_file "%OUT%\45_super_presets.json" -super_preset t45 ^
  -of "%OUT%\45_from_movie_strip" -filmstrip_tiles 6 > "%OUT%\45_movie_input.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py -i "%OUT%\44_ICOI2_comp_v000.mov" -preview "%OUT%\45_preview.png" ^
  -thumbnail_frame 1003 -burn_preset none -burn_tl "{current_frame} {tc}" >> "%OUT%\45_movie_input.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
type "%OUT%\45_movie_input.log"
call :check %RC% "45 movie input"
call :expect "%OUT%\45_movie_input.log" "Input: movie" "45 movie probed"
call :expect "%OUT%\45_movie_input.log" "frames numbered 1001-" "45 movie frame numbers"
call :expect "%OUT%\45_movie_input.log" " (movie)" "45 movie frame rate and timecode from the movie"
call :expect "%OUT%\45_movie_input.log" "Movie: unpacking frame 1003" "45 preview unpacks one frame"
call :expect "%OUT%\45_movie_input.log" "Letterbox" "45 super-preset letterbox"
if not exist "%OUT%\45_from_movie.mp4" (
  >> "%SUMMARY%" echo [FAIL] 45 movie input - 45_from_movie.mp4 missing
  set /a FAILED+=1
)
if exist "%OUT%\45_from_movie.mp4" (>> "%SUMMARY%" echo [INFO] 45 movie input - check 45_from_movie.mp4: slate, burn-in frame numbers from 1001, 2.39 letterbox; 45_preview.png shows frame 1003)

echo.
echo ==== 46  image sequence output: EXR to JPG and EXR to EXR with oiiotool only, trimmed, oiiotool letterbox ====
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\46_jpg\{clean_name}.####.jpg" -in 1002 -out 1004 -width 0 ^
  -oiio_letterbox 1 -oiio_letterbox_ratio 2.39 -oiio_transform_args "--resize 1280x0" > "%OUT%\46_sequences.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py ^
  -i "%MEDIA%\ICOI2_005_033_renderCompSecondary_v000\ICOI2_005_033_renderCompSecondary_v000.1001.exr" ^
  -o "%OUT%\46_exr\plate.%%05d.exr" -in 1001 -out 1002 -seq_args "-d half --compression dwab:45" ^
  >> "%OUT%\46_sequences.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
type "%OUT%\46_sequences.log"
call :check %RC% "46 image sequence output"
call :expect "%OUT%\46_sequences.log" "made by oiiotool (no ffmpeg)" "46 oiiotool only"
call :expect "%OUT%\46_sequences.log" "Trim: frames 1002-1004" "46 trim"
call :expect "%OUT%\46_sequences.log" "oiiotool letterbox 2.39" "46 oiiotool letterbox"
call :expect_not "%OUT%\46_sequences.log" "Encoding" "46 no ffmpeg encode"
if not exist "%OUT%\46_jpg\ICOI2_005_033_renderCompSecondary_v000.1003.jpg" (
  >> "%SUMMARY%" echo [FAIL] 46 image sequence output - 46_jpg frame 1003 missing
  set /a FAILED+=1
)
if not exist "%OUT%\46_exr\plate.01002.exr" (
  >> "%SUMMARY%" echo [FAIL] 46 image sequence output - 46_exr\plate.01002.exr missing
  set /a FAILED+=1
)
if exist "%OUT%\46_jpg\ICOI2_005_033_renderCompSecondary_v000.1003.jpg" (>> "%SUMMARY%" echo [INFO] 46 image sequence output - check 46_jpg: 1280 wide, 2.39 bars, same colours as the movies; 46_exr: linear like the source)

echo.
echo ==== 47  movie to JPG sequence with ffmpeg only: trim, ffmpeg transform, burn-in ====
"%PY%" vfx-transcode.py -i "%OUT%\44_ICOI2_comp_v000.mov" -o "%OUT%\47_jpg\frame.####.jpg" ^
  -in 1002 -out 1003 -ff_transform "scale=w=960:h=-2:flags=lanczos" -burn_preset none -burn_tl "{current_frame}" ^
  > "%OUT%\47_movie_to_jpg.log" 2>&1
set RC=%ERRORLEVEL%
type "%OUT%\47_movie_to_jpg.log"
call :check %RC% "47 movie to jpg"
call :expect "%OUT%\47_movie_to_jpg.log" "made by ffmpeg (no oiiotool)" "47 ffmpeg only"
if not exist "%OUT%\47_jpg\frame.1003.jpg" (
  >> "%SUMMARY%" echo [FAIL] 47 movie to jpg - frame.1003.jpg missing
  set /a FAILED+=1
)

echo.
echo ==== 48  -alpha auto: the ProRes 4444 of test 37 to a PNG sequence with alpha, to h264 without ====
"%PY%" vfx-transcode.py -i "%OUT%\37_alpha_straight.mov" -o "%OUT%\48_png\frame.####.png" ^
  -in 1001 -out 1003 -alpha auto > "%OUT%\48_alpha_auto.log" 2>&1
set RC=%ERRORLEVEL%
"%PY%" vfx-transcode.py -i "%OUT%\37_alpha_straight.mov" -o "%OUT%\48_alpha_auto_h264.mp4" ^
  -alpha auto >> "%OUT%\48_alpha_auto.log" 2>&1
if not "%ERRORLEVEL%"=="0" set RC=%ERRORLEVEL%
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -i "%OUT%\48_png\frame.1001.png" > "%OUT%\48_alpha_probe.txt" 2>&1
type "%OUT%\48_alpha_auto.log"
call :check %RC% "48 alpha auto"
call :expect "%OUT%\48_alpha_auto.log" "Alpha: kept" "48 alpha auto png"
call :expect "%OUT%\48_alpha_auto.log" "Alpha: not kept (-alpha auto)" "48 alpha auto h264"
call :expect "%OUT%\48_alpha_probe.txt" "rgba" "48 alpha png"

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
