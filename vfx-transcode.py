import argparse
import concurrent.futures
import fileseq
import json
import multiprocessing
import os
from pathlib import Path
import pprint
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree
from time import perf_counter

# Regex to parse array attributes
ARRAY_TYPE_REGEX = re.compile(r"^(int|float|string)\[\d+\]$")

STRING_TAGS = {
    "format"
}
INT_TAGS = {
    "x", "y", "z",
    "width", "height", "depth",
    "full_x", "full_y", "full_z",
    "full_width", "full_height", "full_depth",
    "tile_width", "tile_height", "tile_depth",
    "nchannels",
    "alpha_channel",
    "z_channel",
    "deep",
    "subimages",
}
XML_CHAR_REF_REGEX_HEX = re.compile(r"&#x?[0-9a-fA-F]+;")

PREFERRED_CHANNELS = [
    ['rgba.R', 'rgba.G', 'rgba.B', 'rgba.A'],
    ['rgba.R', 'rgba.G', 'rgba.B'],
    ['rgb.R', 'rgb.G', 'rgb.B', 'rgb.A'],
    ['rgb.R', 'rgb.G', 'rgb.B'],
    ['R', 'G', 'B', 'A'],
    ['r', 'g', 'b', 'a'],
    ['R', 'G', 'B'],
    ['r', 'g', 'b'],
    ['beauty.R', 'beauty.G', 'beauty.B', 'beauty.A'],
    ['beauty.R', 'beauty.G', 'beauty.B']
]


class RationalToInt:
    """Rational value stored as division of 2 integers using string."""

    def __init__(self, string_value):
        parts = string_value.split("/")
        top = float(parts[0])
        bottom = 1.0
        if len(parts) != 1:
            bottom = float(parts[1])

        self._value = float(top) / float(bottom)
        self._string_value = string_value

    @property
    def value(self):
        return self._value

    @property
    def string_value(self):
        return self._string_value

    def __format__(self, *args, **kwargs):
        return self._string_value.__format__(*args, **kwargs)

    def __float__(self):
        return self._value

    def __str__(self):
        return self._string_value

    def __repr__(self):
        return "<{}> {}".format(self.__class__.__name__, self._string_value)


def convert_value_by_type_name(value_type, value):
    """Convert value to proper type based on type name.

    In some cases value types have custom python class.
    """

    # Simple types
    if value_type == "string":
        return value

    if value_type == "int":
        return int(value)

    if value_type in ("float", "double"):
        return float(value)

    # Vectors will probably have more types
    if value_type in ("vec2f", "float2", "float2d"):
        return [float(item) for item in value.split(",")]

    # Matrix should be always have square size of element 3x3, 4x4
    # - are returned as list of lists
    if value_type in ("matrix", "matrixd"):
        output = []
        current_index = -1
        parts = value.split(",")
        parts_len = len(parts)
        if parts_len == 1:
            divisor = 1
        elif parts_len == 4:
            divisor = 2
        elif parts_len == 9:
            divisor = 3
        elif parts_len == 16:
            divisor = 4
        else:
            for part in parts:
                output.append(float(part))
            return output

        for idx, item in enumerate(parts):
            list_index = idx % divisor
            if list_index > current_index:
                current_index = list_index
                output.append([])
            output[list_index].append(float(item))
        return output

    if value_type == "rational2i":
        return RationalToInt(value)

    if value_type in ("vector", "vectord"):
        parts = [part.strip() for part in value.split(",")]
        output = []
        for part in parts:
            if part == "-nan":
                output.append(None)
                continue
            try:
                part = float(part)
            except ValueError:
                pass
            output.append(part)
        return output

    if value_type == "timecode":
        return value

    # Array of other types is converted to list
    re_result = ARRAY_TYPE_REGEX.findall(value_type)
    if re_result:
        array_type = re_result[0]
        output = []
        for item in value.split(","):
            output.append(
                convert_value_by_type_name(array_type, item)
            )
        return output

    return value


def parse_oiio_xml_output(xml_string):
    """Parse xml output from OIIO info command."""
    output = {}
    if not xml_string:
        return output

    # Fix values with ampresand (lazy fix)
    # - oiiotool exports invalid xml which ElementTree can't handle
    #   e.g. "&#01;"
    # WARNING: this will affect even valid character entities. If you need
    #   those values correctly, this must take care of valid character ranges.
    #   See https://github.com/pypeclub/OpenPype/pull/2729
    matches = XML_CHAR_REF_REGEX_HEX.findall(xml_string)
    for match in matches:
        new_value = match.replace("&", "&amp;")
        xml_string = xml_string.replace(match, new_value)

    tree = xml.etree.ElementTree.fromstring(xml_string)
    attribs = {}
    output["attribs"] = attribs
    for child in tree:
        tag_name = child.tag
        if tag_name == "attrib":
            attrib_def = child.attrib
            value = convert_value_by_type_name(
                attrib_def["type"], child.text
            )

            attribs[attrib_def["name"]] = value
            continue

        # Channels are stored as tex on each child
        if tag_name == "channelnames":
            value = []
            for channel in child:
                value.append(channel.text)

        # Convert known integer type tags to int
        elif tag_name in INT_TAGS:
            value = int(child.text)

        # Keep value of known string tags
        elif tag_name in STRING_TAGS:
            value = child.text

        # Keep value as text for unknown tags
        # - feel free to add more tags
        else:
            value = child.text

        output[child.tag] = value

    return output


def get_oiio_info_for_input(my_args, subimages=True):
    """Call oiiotool to get information about input and return stdout.

    Stdout should contain xml format string.
    """
    def run_subprocess(*args, **kwargs):

        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["stdin"] = subprocess.PIPE
        proc = subprocess.Popen(*args, **kwargs)
        full_output = ""
        _stdout, _stderr = proc.communicate()
        if _stdout:
            _stdout = _stdout.decode("utf-8", errors="backslashreplace")
            full_output += _stdout
        if _stderr:
            _stderr = _stderr.decode("utf-8", errors="backslashreplace")
            if full_output:
                full_output += "\n"
            full_output += _stderr
        if proc.returncode != 0:
            exc_msg = "Executing arguments was not successful: \"{}\"".format(
                args)
            if _stdout:
                exc_msg += "\n\nOutput:\n{}".format(_stdout)
            if _stderr:
                exc_msg += "Error:\n{}".format(_stderr)
        return full_output


    filepath = my_args['i']
    args = [
        my_args['oiio'],
        "--info",
        "-v"
    ]
    if subimages:
        args.append("-a")

    args.extend(["-i:infoformat=xml", filepath])

    output = run_subprocess(args)
    output = output.replace("\r\n", "\n")

    xml_started = False
    subimages_lines = []
    lines = []
    for line in output.split("\n"):
        if not xml_started:
            if not line.startswith("<"):
                continue
            xml_started = True

        if xml_started:
            lines.append(line)
            if line == "</ImageSpec>":
                subimages_lines.append(lines)
                lines = []

    if not xml_started:
        raise ValueError(
            "Failed to read input file \"{}\".\nOutput:\n{}".format(
                filepath, output
            )
        )

    output = []
    for subimage_lines in subimages_lines:
        xml_text = "\n".join(subimage_lines)
        output.append(parse_oiio_xml_output(xml_text))

    if subimages:
        return output
    return output[0]


def _prepare_filter(args):

    in_filter = args['ff_filter']

    scale = 'scale={}:0'.format(str(args['width']))

    # pads to even number if height is bad
    scale += ',pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2'
    if in_filter != '':
        scale += ',{}'.format(str(in_filter))
    burn = _prepare_burnin(args)
    if burn != '':
        if scale != '':
            scale += ','
        scale += str(burn)

    v_filter = f"-filter:v [in]{scale}[out]"
    return v_filter


def _prepare_burnin(args):

    clean_name = args['clean_name_no_sep']
    tc = args['timecode']
    fps = args['fps']
    frame_start = args['start']
    frame_end = args['end']
    font_path = args['burn_font']
    style = 'fontcolor=white:shadowcolor=black:shadowx=2:shadowy=1'
    pos_tl = 'x=w*0.01:y=lh'
    pos_tr = 'x=w-w*0.01-tw:y=lh'
    pos_bl = 'x=w*0.01:y=h-(2*lh)'
    pos_br = 'x=w-w*0.01-tw:y=h-(2*lh)'
    txt_tl = args['burn_tl']
    txt_tr = args['burn_tr']
    txt_bl = args['burn_bl']
    txt_br = args['burn_br']

    def make_keywords(tc, frame_start, frame_end, fps):
        keywords = {
            'tc': "timecode=\'" + tc.replace(':', '\\:') + "\': r=" + str(fps) + ':',
            'current_frame': "\'%{frame_num}\'",
            'frame_start': frame_start,
            'frame_end': frame_end,
            'clean_name' : clean_name
        }
        return keywords

    def replace_text(txt, keywords, frame_start):
        expanded = txt
        try:
            expanded = txt.format(**keywords)
        except KeyError as e:
            print("KeyError: " + str(e))
            pass
        if '{current_frame}' in txt:
            return "start_number=" + str(frame_start) + ":text=" + expanded
        else:
            return "text=\'" + expanded + "\'"

    if font_path != '':
        font_file = "fontfile='" + font_path.replace(':', '\\:') + "\':"
    else:
        font_file = ''

    burn_size = '(h/{})'.format(str(args['burn_size']))
    burn_style = 'fontsize={}:{}'.format(burn_size, style)
    keywords = make_keywords(tc, frame_start, frame_end, fps)

    burn = ''
    if txt_tl != '':
        if burn != "":
            burn += ","
        rpl = replace_text(txt_tl, keywords, frame_start)
        burn += "drawtext=" + font_file + burn_style + ":" + rpl + ":" + pos_tl
    if txt_tr != '':
        if burn != "":
            burn += ","
        rpl = replace_text(txt_tr, keywords, frame_start)
        burn += "drawtext=" + font_file + burn_style + ":" + rpl + ":" + pos_tr
    if txt_bl != '':
        if burn != "":
            burn += ","
        rpl = replace_text(txt_bl, keywords, frame_start)
        burn += "drawtext=" + font_file + burn_style + ":" + rpl + ":" + pos_bl
    if txt_br != '':
        if burn != "":
            burn += ","
        rpl = replace_text(txt_br, keywords, frame_start)
        burn += "drawtext=" + font_file + burn_style + ":" + rpl + ":" + pos_br

    return burn

def frame_to_tc(frames, fps=24):
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(int(frames / fps / 3600), int((frames / fps) % 3600 / 60), int(frames / fps) % 60, frames % (fps*60) % fps)

def find_sequence_from_file(args):

    input_file = args['i'].replace('\\', '/')
    input_path = Path(input_file).parent

    if not input_file:
        raise ValueError(f"No EXR files found in {input_path}")

    # parse the file like noob
    _pth = os.path.dirname(input_file)
    _name = os.path.basename(input_file)
    _name, _ext = os.path.splitext(_name)
    NM_RE = re.compile(r".*?(?P<snm>\d+)$")
    m = NM_RE.search(_name)
    if m != None:
        _number_string = m.group("snm")
        _padding = len(_number_string)
        _number = int(_number_string)
        CN_RE = re.compile(r"(?P<cn>.*?)\d+$")
        n = CN_RE.search(_name)
        if n:
            _clean_name = n.group("cn")
    else:
        _clean_name = _name
    _clean_name_no_sep = _clean_name
    _clean_name_sep_char = ''
    if _number is not None and len(_clean_name) > 0:
        if _clean_name[-1] == '.':
            _clean_name_no_sep = _clean_name[:-1]
            _clean_name_sep_char = '.'
        elif _clean_name[-1] == '_':
            _clean_name_no_sep = _clean_name[:-1]
            _clean_name_sep_char = '_'
        elif _clean_name[-1] == '-':
            _clean_name_no_sep = _clean_name[:-1]
            _clean_name_sep_char = '-'

    pattern = _pth + '/' + _clean_name_no_sep + _clean_name_sep_char + '#' + _ext
    sequence = fileseq.findSequencesOnDisk(pattern)
    if sequence:
        seq = fileseq.FileSequence(sequence)
        fs = seq.frameSet()
        more_args = {
            'input_sequence': list(sequence[0]),
            'start': fs.start(),
            'tc_from_start': frame_to_tc(fs.start(), args['fps_int']),
            'end': fs.end(),
            'zfill': seq._zfill,
            'input_base': _clean_name,
            'input_base_full': _pth + '/' + _clean_name,
            'clean_name': _clean_name,
            'clean_name_no_sep': _clean_name_no_sep,
            'clean_name_sep_char': _clean_name_sep_char,
        }

        return more_args
    else:
        raise ValueError(f"Unable to recognize the sequence pattern in {input_path}")


def convert_one_exr(one_command):

    one_start = perf_counter()
    args = one_command['args']
    cmd = one_command['cmd']
    my_file = one_command['file']

    if int(args['verbose']) and int(args['verbose']) == 1:
        print("Converting file {}".format(my_file))
    elif int(args['verbose']) and int(args['verbose']) >= 1:
        print("Converting file {} with command {}".format(my_file, cmd))

    result = subprocess.run(one_command['cmd'],
                            text=True,
                            capture_output=True)
    one_end = perf_counter()
    if result.returncode != 0:
        if args['verbose'] <= 1:
            print("Converting file {} failed.".format(my_file))
        elif args['verbose'] >= 1:
            print("Converting file {} failed with status {}".format(my_file, result.returncode))
            print(f"stdout: {result.stdout}")
            print(f"stderr: {result.stderr}")

        raise subprocess.CalledProcessError(
            result.returncode, one_command['cmd'])
    else:
        print(f"Conversion OK: {one_command['file']}. {(one_end - one_start):.2f} second(s)")


def convert_exr_to_mezzanine(args):

    input_sequence = args['input_sequence']
    input_folder = args['input_folder']
    input_path = Path(input_folder)
    output_folder = args['output_folder']
    output_path = Path(output_folder)

    if int(args['verbose']) and int(args['verbose']) == 1:
        print("Converting {} files".format(len(input_sequence)))
    elif int(args['verbose']) and int(args['verbose']) >= 1:
        print("Converting {} files".format(len(input_sequence)))
        print("The input sequence: {}".format(input_sequence))

    output_path.mkdir(parents=True, exist_ok=True)
    if int(args['verbose']) and int(args['verbose']) >= 1:
        print("Creating output path: {}".format(output_folder))

    command_mid = [
        '--colorconvert',
        args['colorin'],
        args['colorout']
    ]
    if args['width'] != 0:
        command_mid.append('--resize')
        command_mid.append('{}x0'.format(args['width']))

    # add user oiio commands
    if args['oiio_command'] != "":
        command_mid.extend(args['oiio_command'].strip().split(" "))

    # which format to output to
    out_ext = args['mezzanine'].lower().strip()
    if out_ext is None or out_ext == "" or len(out_ext) < 2:
        # sensible default
        out_ext = "png"
    out_ext = '.' + out_ext

    # prepare commands for all input images
    commands = []
    for frame in input_sequence:
        exr_file = input_path / frame
        output_file = output_path / exr_file.with_suffix(out_ext).name
        command = [args['oiio'], '-i',
                   str(exr_file).replace('\\', '/'), '--ch', args['picked_channel_no_alpha']]
        command.extend(command_mid)
        command.append('-o')
        command.append(str(output_file).replace('\\', '/'))
        filename = os.path.basename(exr_file)
        commands.append({
            'cmd': command,
            'file': filename,
            'output_file': output_file,
            'args': args
        })

    if int(args['verbose']) and int(args['verbose']) >= 0:
        print("Executing EXR conversion with {} threads".format(args['cpu_count']))
        print("The oiio command:\n{}".format(" ".join(commands[0]['cmd'])))
        print("The oiio command:\n{}".format(" ".join(commands[0]['cmd'])))

    oiio_start = perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args['cpu_count']) as executor:
        executor.map(convert_one_exr, commands)
    oiio_end = perf_counter()
    print(f"Conversion to {args['mezzanine']} took {(oiio_end - oiio_start):.2f} seconds")

    ifl = '# ifl for ffmpeg\n'
    one_frame_duration = float(int(args['fps_div'][1])) / float(int(args['fps_div'][0]))
    for one in commands:
        ifl += 'file ' + str(os.path.basename(one['output_file'])) + '\nduration ' + str(one_frame_duration) + '\n'

    args['ifl'] = str(commands[0]['output_file'])+'.txt'
    with open(args['ifl'], 'w') as f:
        f.write(ifl)


def convert_mezzanine_sequence_to_container(args):

    can_do_tc = ["mov", "mp4"]

    output_folder = args['output_folder']
    input_file_basefolder = args['input_file_base_folder']
    start = str(args['start'])
    frame_rate = str(args['fps'])

    new_output_file = os.path.join(input_file_basefolder, f"{args['clean_name_no_sep']}.{args['ff_out_ext']}")
    if args['o'] != '':
        new_output_file = args['o']
    input_path = Path(output_folder)
    command = [
        args['ffmpeg'],
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-r",
        frame_rate,
        "-safe",
        "0",
        "-i"
    ]

    if args['ext'] == '.exr':
        ifl = args.get('ifl')
        if ifl is None:
            ifl = str(input_path / f"{args['clean_name']}{start}.{args['mezzanine']}.txt")
        command.append(ifl)
    else:
        command.append(str(args['input_printf']))

    command.append(_prepare_filter(args))

    command.extend(args['ff_encode'].split())
    #if args['ff_out_ext'] in can_do_tc and bool(int(args['output_timecode'])):
    #    command.extend(["-timecode", args['timecode']])
    command.append(new_output_file)

    print(f"Running command: {' '.join(command)}")
    result = subprocess.run(command, check=False, capture_output=True, text=True)

    if result.returncode != 0:
        if int(args['verbose']) and int(args['verbose']) <= 1:
            print("FFmpeg failed with exit status {}.".format(result.returncode))
        elif int(args['verbose']) and int(args['verbose']) >= 1:
            print("FFmpeg failed with exit status {}.".format(result.returncode))
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, " ".join(command))


def clean_temp_folder(args):

    if args['output_folder'] != args['input_folder']:
        shutil.rmtree(args['output_folder'])


def get_args():

    parser = argparse.ArgumentParser(description="Converts Open EXRs to h264 mp4.\n Version 09 2024")
    parser.add_argument(
        '-i',
        help="The input file to convert. Required.",
        type=str,
        required=True
    )
    parser.add_argument(
        '-r',
        '--fps',
        help="Frame rate, in form *24, 25, 24000/1001",
        type=str,
        default='24',
        metavar=''
    )
    parser.add_argument(
        '-ci',
        '--colorin',
        help='Select EXR (input) colorspace: *ACEScg, \"Utility - Linear - sRGB\"',
        type=str,
        default='ACEScg',
        metavar=''
    )
    parser.add_argument(
        '-co',
        '--colorout',
        help='Select output colorspace: *out_rec709, out_srgb',
        type=str,
        default='out_rec709',
        metavar=''
    )
    parser.add_argument(
        "-v",
        "--verbose",
        default=0,
        type=str,
        help="Verbose description."
    )
    parser.add_argument(
        '-ffmpeg',
        help="Path to ffmpeg executable. Use forward slashes",
        type=str,
        required=False,
        default='./ffmpeg/ffmpeg.exe'
    )
    parser.add_argument(
        '-skipffmpeg',
        help='Can turn off the ffmpeg part, will force -temp_keep 1.',
        type=str,
        default='0'
    )
    parser.add_argument(
        '-oiio',
        help="Path to oiiotool executable. Use forward slashes",
        type=str,
        required=False,
        default='./oiio/oiiotool.exe'
    )
    parser.add_argument(
        '-oiio_threads',
        help="Forces to use number of threads. Zero sets it to the number of cores machine has.",
        type=int,
        required=False,
        default=0
    )
    parser.add_argument(
        '-oiio_channel',
        help="Forces to use chanel name for conversion. Defaults to empty, can be R,G,B,A",
        type=str,
        required=False,
        default=""
    )
    parser.add_argument(
        '-oiio_channel_use_first',
        help="Forces to use chanel name for conversion. Defaults to empty, can be R,G,B,A",
        type=int,
        required=False,
        default=0
    )
    parser.add_argument(
        '-oiio_command',
        help="To be added after colorconvert and resize commands",
        type=str,
        required=False,
        default=""
    )
    parser.add_argument(
        '-ocio',
        help="Path to ocio config to be used. Use forward slashes",
        type=str,
        required=False,
        default='./ocio/aces_1.2cg/config.ocio'
    )
    parser.add_argument(
        '-temp',
        help="Temporary folder to be created inside input folder",
        type=str,
        required=False,
        default='.temp_png_sequence'
    )
    parser.add_argument(
        '-temp_keep',
        help='Can turn off deleting the intermediate folder with oiio conversion with -temp_keep 1.',
        type=str,
        default='0'
    )
    parser.add_argument(
        '-mezzanine',
        help='File extension for mezzanine convert from exr by oiiotool.',
        type=str,
        default='png'
    )
    parser.add_argument(
        '-width',
        help="Resize the image to the defined width. Use 0 for no resize. Defaults to 1080",
        type=int,
        required=False,
        default=1920
    )
    parser.add_argument(
        '-timecode',
        help="Set Timecode start. Defaults to 01:00:00:00",
        type=str,
        required=False,
        default='01:00:00:00'
    )
    parser.add_argument(
        '-timecode_from_counter',
        help="Sets the timecode from the frame counter. Frame 1001 is 00:00:41:17 at 24 fps. Defaults to 1. Overrides -timecode.",
        type=str,
        required=False,
        default='1'
    )
    parser.add_argument(
        '-output_timecode',
        help="Outputs timecode in the output file. Defaults to 1.",
        type=str,
        required=False,
        default='1'
    )
    parser.add_argument(
        '-burn_size',
        help="Burn it text size. Defaults to 75",
        type=int,
        required=False,
        default=75
    )
    parser.add_argument(
        '-burn_font',
        help="Burn-in font path.",
        type=str,
        required=False,
        default='./fonts/DroidSansMono.ttf'
    )
    parser.add_argument(
        '-burn_tl',
        help="Burn-in top left text.",
        type=str,
        required=False,
        default=''
    )
    parser.add_argument(
        '-burn_tr',
        help="Burn-in top right text.",
        type=str,
        required=False,
        default=''
    )
    parser.add_argument(
        '-burn_bl',
        help="Burn-in bottom left text. Use {clean_name} for input name.",
        type=str,
        required=False,
        default=''
    )
    parser.add_argument(
        '-burn_br',
        help="Burn-in bottom right text. Use {current_frame} for counter.",
        type=str,
        required=False,
        default=''
    )
    parser.add_argument(
        '-ff_filter',
        help="FFmpeg encoding filter. Defaults to colorspace filter conversion.",
        type=str,
        required=False,
        default=''
    )
    parser.add_argument(
        '-ff_encode',
        help="FFmpeg encoding to be put after filtering and before output file. Defaults to nice h264.",
        type=str,
        required=False,
        default='-c:v libx264 -profile:v main -g 1 -tune stillimage -crf 18 -pix_fmt yuv420p -color_primaries bt709 -color_trc bt709 -colorspace bt709'
    )
    parser.add_argument(
        '-ff_out_ext',
        help="Output file extension for FFmpeg encoding. Defaults to mp4.",
        type=str,
        required=False,
        default='mp4'
    )
    parser.add_argument(
        '-write_exr_info',
        help="Write output of oiiotool to json next to the exr file. Use -write_exr_info 1",
        type=str,
        required=False,
        default= '0'
    )
    parser.add_argument(
        '-o',
        help="Explicit output path and file name. If empty, will use input name.",
        type=str,
        required=False,
        default=''
    )
    return parser.parse_args()

def pick_preferred_channels(args):

    more_args = {}
    channel_names = []
    more_args['oiio_width'] = 0
    more_args['oiio_height'] = 0
    for one in args['info']:
        channel_names.extend(one.get('channelnames'))
        _w = one.get("width", None)
        if _w:
            more_args['oiio_width'] = _w
        _h = one.get("height", None)
        if _h:
            more_args['oiio_height'] = _h
    more_args['channel_names'] = channel_names
    # typical ['AO.R', 'AO.G', 'AO.B', 'AO.A', 'rgba.R', 'rgba.G', 'rgba.B', 'rgba.A']
    #print(f"channel_names 1 : {channel_names}")

    if len(args['info']) >= 1 and len(channel_names[0]) >= 4:
        # all channel names are now in one list. Let's separate
        separated = []
        current_prefix = ''
        prefixes = []
        current_channel = []
        for one in channel_names:
            cp = one.split('.')[0]
            if not current_channel:
                current_channel.append(one)
                current_prefix = cp
                prefixes.append(current_prefix)
            else:
                if cp != current_prefix:
                    # new channel
                    separated.append(current_channel)
                    current_channel = [one]
                    current_prefix = cp
                    prefixes.append(current_prefix)
                else:
                    current_channel.append(one)
        separated.append(current_channel)
        channel_names = separated
    print(f"Channel_names separated : {channel_names}")

    if args['oiio_channel_preferred'] in channel_names:
        more_args['picked_channel'] = ','.join(args['oiio_channel_preferred'])
        print(f"Channel picked by argument : {more_args['picked_channel']}")
    elif args['oiio_channel_preferred'] != '':
        more_args['picked_channel'] = ','.join(args['oiio_channel_preferred'])
        print(f"Channel picked by argument : {more_args['picked_channel']}\nWarning: channels not present.")
    else:
        # pick best chanel
        more_args['picked_channel'] = ''
        for one in channel_names:
            for one_preferred in PREFERRED_CHANNELS:
                if one == one_preferred:
                    more_args['picked_channel'] = ','.join(one)
                    break
        print(f"Channel picked by guess : {more_args['picked_channel']}")

    # if in doubt, pick first layer
    if more_args['picked_channel'] == '':
        more_args['picked_channel'] = ','.join(channel_names[0])


        # if first layer has single channel, try to pick first 3
        if ',' in more_args['picked_channel']:
            pass
        elif len(channel_names) >= 3:
            more_args['picked_channel'] = '{},{},{}'.format(str(channel_names[0][0]), str(channel_names[1][0]), str(channel_names[2][0]))
        print(
            f"Channel picker guess failed, picking first channel : {more_args['picked_channel']}")

    # try to remove alpha
    no_a = re.match("(.*)(,.*[\\.]*[a|A])", more_args['picked_channel'])
    if no_a:
        more_args['picked_channel_no_alpha'] = no_a.group(1)
        print(
            f"Channel picker removed alpha : {more_args['picked_channel_no_alpha']}")
    else:
        more_args['picked_channel_no_alpha'] = more_args['picked_channel']

    if int(args['verbose']) and int(args['verbose']) >= 1:
        print("Channels found:")
        pprint.pprint(channel_names)
        print("Channels picked:")
        pprint.pprint(more_args['picked_channel_no_alpha'])

    # get other important image info
    return more_args

def get_current_folder():
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle, the PyInstaller bootloader
        # extends the sys module by a flag frozen=True and sets the app
        # path into variable _MEIPASS'.
        # self.application_path = sys._MEIPASS.replace('\\', '/')
        application_path = os.path.dirname(sys.executable).replace('\\', '/')
    else:
        application_path = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
    return application_path

def make_path_absolute(path, app_path):
    if path.startswith('.'):
        path = app_path + path[1:]
    return path

def main():
    """
    log = logging.getLogger("mylog")
    log_fname = os.path.join(_get_script_path(), 'vfx-transcode.log')
    logging.basicConfig(filename=log_fname, level=logging.DEBUG)
    log.info('Started at: ' + time.strftime("%Y-%m-%d, %H:%M"))
    """
    args = vars(get_args())

    if '/' in args['fps']:
        fa = args['fps'].split('/')[0]
        fb = args['fps'].split('/')[1]
    else:
        fa = str(int(args['fps'])*1000)
        fb = '1000'
    args['fps_div'] = [fa, fb]
    args['fps_int'] = int(round((float(fa) / float(fb)),0))

    # parse input sequence
    more_args = find_sequence_from_file(args)
    args = {**args, **more_args}

    # parse args

    # replace timecode by
    if bool(int(args['timecode_from_counter'])):
        args['timecode'] = args['tc_from_start']

    # if path starts with dot, replace by path of the __file__
    app_path = get_current_folder()
    args['burn_font'] = make_path_absolute(args['burn_font'], app_path)
    args['ocio'] = make_path_absolute(args['ocio'], app_path)
    args['oiio'] = make_path_absolute(args['oiio'], app_path)
    args['ffmpeg'] = make_path_absolute(args['ffmpeg'], app_path)

    # make sure to keep temp
    skip_ffmpeg = False
    if bool(int(args['skipffmpeg'])):
        args['temp_keep'] = '1'
        skip_ffmpeg = True
    args['input_file_base_folder'] = os.path.dirname(args['i'])
    args['input_folder'] = os.path.dirname(args['input_sequence'][0])
    if args['temp'] and args['temp'] != '':
        args['output_folder'] = os.path.join(args['input_folder'], args['temp'])
    else:
        args['output_folder'] = args['input_folder']
    filename, ext = os.path.splitext(os.path.basename(args['input_sequence'][0]))
    args['ext'] = ext.lower()
    args['output_filename'] = args['clean_name_no_sep']
    args['input_printf'] = args['input_folder'] + '\\{}%0{}d{}'.format(
        args['input_base'],
        args['zfill'],
        args['ext']
    ).replace('\\', '/')
    if args['oiio_threads'] == 0:
        args['cpu_count'] = multiprocessing.cpu_count()
    else:
        args['cpu_count'] = args['oiio_threads']

    # set ocio config environment variable
    os.environ['OCIO'] = args['ocio']

    args['oiio_channel_preferred'] = args['oiio_channel'].split(',')
    if args['ext'] == '.exr':

        # use oiio to get all the info from first exr
        info = get_oiio_info_for_input(args)
        args['info'] = info

        if args['write_exr_info'] == '1':
            with open(args['i']+'.json', 'w') as fout:
                json.dump(info, fout, sort_keys=True, indent=4)

        # pick preferred channel, 'picked_channel_no_alpha'
        more_args = pick_preferred_channels(args)
        args = {**args, **more_args}

        # use oiio to convert exr to png
        convert_exr_to_mezzanine(args)

    # convert to video with ffmpeg
    if not skip_ffmpeg:
        video_failed = False
        try:
            convert_mezzanine_sequence_to_container(args)
        except:
            print("Failed creating video {} file.".format(args['ff_out_ext']))

    #pprint.pprint(args)
    # delete intermediate png files
    # temp keep is tring arg parser sucks with bool
    if not (args['temp_keep'] == '1'):
        if args['ext'] == '.exr':
            clean_temp_folder(args)


if __name__ == "__main__":
    #TODO detect failed conversions, copy next, delete copied
    main()
