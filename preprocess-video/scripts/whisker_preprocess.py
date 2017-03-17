#!/usr/bin/env python
"""
Whisker Preprocessor.   Extracts bilateral whisking and eyeblink data from a video snippet.

Usage:
    whisker_preprocess -h | --help
    whisker_preprocess --version
    whisker_preprocess ([-i <input_file> | --input <input_file>] | [ --config <config_file> | --print_config] ) [(-o <output_file> | --output <output_file>)]
                       [(-v | --verbose)] [--clean]

Options:
    -h --help                   Show this screen and exit.
    --version                   Display the version and exit.
    --print_config              Print the default config value and exit.
    -i --input=<input_file>     Specify the file to process.
    -o --output=<output_file>   Specify a location to store the analyzed results.
    --config=<config_file>      Specify a path to a custom config file.  See --print-config for format.
    --clean                     If existing processed videos and analysis data exist, overwrite them with new.
    -v --verbose                Display extra diagnostic information during execution.

"""
import platform
import pprint
import subprocess
import sys
from enum import Enum
from logging import info, error, getLogger, ERROR
from multiprocessing import cpu_count
from os import access, W_OK, utime

import attr
import cv2
import pandas as pd
import progressbar
from attr.validators import instance_of
from attrs_utils.interop import from_docopt
from attrs_utils.validators import ensure_enum
from joblib import Parallel, delayed

import core.eyes as eyes
import core.yaml_config as yaml_config
from core._version import __version__
from core.base import *
from core.whisk_analysis import filter_raw

KEEP_FILES = True


class SideOfFace(Enum):
    left = 1
    right = 2


@attr.s
class VideoFileData(object):
    """
    This class holds the file locations for the source video, analyzed results, and raw eye data.
    """
    name = attr.ib(validator=instance_of(str))
    side = attr.ib(convert=ensure_enum(SideOfFace))
    eye = attr.ib()
    nframes = attr.ib(validator=instance_of(int))

    def __attrs_post_init__(self):
        name, ext = path.splitext(self.name)
        self.basename = name
        self.whiskname = name + ".whiskers"
        self.measname = name + ".measurements"
        self.eyecheck = name + "-eye-checkpoint.csv"
        self.whiskraw = name + "-whisk-raw.csv"
        self.whiskcheck = name + "-whisk-checkpoint.csv"
        self.summaryfile = name + "-summary.xlsx"
        self.labelname = path.splitext(path.basename(name))[0]


@attr.s
class RecordingSessionData(object):
    """
    This class holds all of the analysis results for one video bout
    """
    videos = attr.ib()


@attr.s
class Chunk(object):
    left = attr.ib(validator=instance_of(str))
    right = attr.ib(validator=instance_of(str))
    start = attr.ib(validator=instance_of(int))
    stop = attr.ib(validator=instance_of(int))


def main(inputargs):
    args = from_docopt(docstring=__doc__, argv=inputargs, version=__version__)
    __check_requirements()
    info('read default hardware parameters.')
    app_config = yaml_config.load(args.config)
    if args.print_config:
        print("Detected Configuration Parameters: ")
        pprint.pprint(attr.asdict(app_config), depth=5)
        return 0
    __validate_args(args)
    if args.clean:
        global KEEP_FILES  # ew.
        KEEP_FILES = False

    info(f'processing file {path.split(args.input)[1]}')
    files = segment_video(args, app_config)
    info('Extracting whisk data for each eye')
    result = Parallel(n_jobs=cpu_count() - 1)(delayed(extract_whisk_data)(f, app_config) for f in files.videos)
    print(result)
    # # print(result)
    # for f in files.videos:
    #     extract_whisk_data(f, app_config)
    # plot_left_right(left, right, 'joined.pdf')
    # plot_left_right(left.iloc[500:900], right.iloc[500:900], 'zoomed.pdf')


def estimate_whisking_from_raw_whiskers(video: VideoFileData, config):
    checkpoint = video.whiskraw
    if not (path.isfile(checkpoint) and KEEP_FILES):
        call = [config.system.python27_path, config.system.trace_path, '--input', video.whiskname, '-o', checkpoint]
        info(f'extracting whisker movement from {video.labelname}')
        data = subprocess.run(call, stdout=subprocess.PIPE)
        if data.returncode == 0:
            data = pd.read_csv(checkpoint)
        else:
            raise IOError(f"failed to extract from {video.labelname}")
    else:
        info(f"found existing whisker data for {video.labelname}")
        data = pd.read_csv(checkpoint)

    side = filter_raw(data, config, video.labelname)
    side.to_csv(video.whiskcheck)
    side = side.set_index('frameid')
    joined = side.join(video.eye)
    joined.to_excel(video.summaryfile)


def extract_whisk_data(video: VideoFileData, config):
    base = config.system.whisk_base_path
    trace_path = path.join(base, 'trace.exe')
    trace_args = [video.name, video.whiskname]
    measure_path = path.join(base, 'measure.exe')
    measure_args = ['--face', video.side.name, video.whiskname, video.measname]
    classify_path = path.join(base, 'classify.exe')
    classify_args = [video.measname, video.measname, video.side.name, '--px2mm', '0.04', '-n', '-1']
    reclassify_path = path.join(base, 'reclassify.exe')
    reclassify_args = [video.measname, video.measname, '-n', '-1']
    if not (KEEP_FILES and path.exists(video.whiskname)):
        info(f'tracing whiskers for {video.labelname}')
        istraced = subprocess.run([trace_path, *trace_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        info(f'found existing whiskers file for {video.labelname}')
        istraced = subprocess.CompletedProcess(args=[], returncode=0)  # fake a completed run.
    if istraced.returncode == 0:
        info(f"trace OK for {video.labelname}")
        if not (KEEP_FILES and path.exists(video.measname)):
            info(f'measuring whiskers for {video.labelname}')
            ismeasured = subprocess.run([measure_path, *measure_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            info(f'found existing measurements file for {video.labelname}')
            ismeasured = subprocess.CompletedProcess(args=[], returncode=0)  # fake a completed run.

        if ismeasured.returncode == 0:
            info(f"measure OK for {video.labelname}")
            if not (KEEP_FILES and path.exists(video.measname)):
                info(f'classifying whiskers for {video.labelname}')
                isclassified = subprocess.run([classify_path, *classify_args], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
            else:
                info(f'found existing measurements file for {video.labelname}')
                isclassified = subprocess.CompletedProcess(args=[], returncode=0)  # fake a completed run.

            if isclassified.returncode == 0:
                info(f"classification OK for {video.labelname}")
                if not (KEEP_FILES and path.exists(video.measname)):
                    info(f'reclassifying whiskers for {video.labelname}')
                    isreclassified = subprocess.run([reclassify_path, *reclassify_args], stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE)
                else:
                    info(f'found existing measurements file for {video.labelname}')
                    isreclassified = subprocess.CompletedProcess(args=[], returncode=0)  # fake a completed run.

                if isreclassified.returncode == 0:
                    info(f"reclassification OK for {video.labelname}")
                    info(f"whiskers complete for {video.labelname}")
                    if not path.isfile(video.whiskname) or not path.isfile(video.measname):
                        raise IOError(f"whisker or measurement file was not saved for {video.name}")
                    if not (path.isfile(video.summaryfile) and KEEP_FILES):
                        estimate_whisking_from_raw_whiskers(video, config)
                        # return video
                else:
                    raise IOError(f"reclassifier failed on {video.labelname}")
            else:
                raise IOError(f"classifier failed on {video.labelname}")
        else:
            raise IOError(f"measurement failed on {video.labelname}")
    else:
        raise IOError(f"trace failed on {video.labelname}")


def segment_video(args, app_config):
    """
    Break up a long recording into multiple small ones.
    :param args:
    :param app_config:
    :return:
    """
    name, ext = path.splitext(path.basename(args.input))
    cap = cv2.VideoCapture(args.input)
    framecount = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    chunk = Chunk(left=path.join(args.output, name + "-left" + ext),
                  right=path.join(args.output, name + "-right" + ext),
                  start=0,
                  stop=framecount)
    return RecordingSessionData(videos=split_and_extract_blink(args, app_config, chunk))


def split_and_extract_blink(args, app_config, chunk: Chunk):
    """
    Split a video into left and right face sides, and extract eye areas
    :param chunk:
    :param args:
    :param app_config:
    :return:
    """
    # grab the video
    cap = cv2.VideoCapture(args.input)
    # initialize storage containers
    nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    left = VideoFileData(name=chunk.left, side=SideOfFace.left, eye=[], nframes=nframes)
    right = VideoFileData(name=chunk.right, side=SideOfFace.right, eye=[], nframes=nframes)

    # jump to the right frame
    cap.set(1, chunk.start)
    codec = cv2.VideoWriter_fourcc(*'MPEG')
    framerate = app_config.camera.framerate

    # get video dimensions
    size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    # compute dimensions of a vertical split
    cropped_size = (round(size[0] / 2), size[1])
    # open file handles for left and right videos
    if not (path.isfile(left.name) and path.isfile(right.name) and path.isfile(right.eyecheck) and path.isfile(
            left.eyecheck) and KEEP_FILES):
        info('Extracting left and right sides...')
        info('Detecting eye areas...')
        vw_left = cv2.VideoWriter(filename=left.name, fourcc=codec, fps=framerate, frameSize=cropped_size,
                                  isColor=False)
        vw_right = cv2.VideoWriter(filename=right.name, fourcc=codec, fps=framerate, frameSize=cropped_size,
                                   isColor=False)
        curframe = 0
        with progressbar.ProgressBar(min_value=0, max_value=nframes) as pb:
            while cap.isOpened():
                ret, frame = cap.read()
                if ret and (curframe < nframes):
                    pb.update(curframe)
                    # split in half
                    left_frame = frame[0:cropped_size[1], 0:cropped_size[0]]
                    right_frame = frame[0:cropped_size[1], cropped_size[0]:size[0]]
                    # measure eye areas
                    left.eye.append((curframe, *eyes.process_frame(left_frame)))
                    right.eye.append((curframe, *eyes.process_frame(right_frame)))
                    # greyscale and invert for whisk detection
                    left_frame = cv2.bitwise_not(cv2.cvtColor(left_frame, cv2.COLOR_BGR2GRAY))
                    right_frame = cv2.bitwise_not(cv2.cvtColor(right_frame, cv2.COLOR_BGR2GRAY))
                    # write out
                    vw_left.write(left_frame)
                    vw_right.write(right_frame)
                    curframe += 1
                    # uncomment to see live preview.
                    # cv2.imshow('left', left)
                    # cv2.imshow('right', right)
                    # if cv2.waitKey(1) & 0xFF == ord('q'):
                    #    break
                else:
                    break
            # clean up, because openCV is stupid and doesn't implement `with ...`
            cap.release()
            vw_left.release()
            vw_right.release()
            cv2.destroyAllWindows()
            # make checkpoint eye data
            left.eye = pd.DataFrame(left.eye, columns=('frameid', 'total_area', 'eye_area'))
            left.eye = left.eye.set_index('frameid')
            right.eye = pd.DataFrame(right.eye, columns=('frameid', 'total_area', 'eye_area'))
            right.eye = right.eye.set_index('frameid')
            info('Saved eye data checkpoint file.')
            left.eye.to_csv(left.eyecheck)
            right.eye.to_csv(right.eyecheck)
    else:
        info('Found existing split video.  Importing existing eye data checkpoint files.')
        left.eye = pd.read_csv(left.eyecheck)
        right.eye = pd.read_csv(right.eyecheck)
    # either return or die.
    if path.isfile(left.name) and path.isfile(right.name):
        aligned_l = __align_timestamps(left.name, args, app_config)
        aligned_r = __align_timestamps(right.name, args, app_config)
        if path.isfile(aligned_l) and path.isfile(aligned_r):
            left.name = aligned_l
            right.name = aligned_r
            info("wrote {0}".format(left.name))
            info("wrote {0}".format(right.name))
            return left, right
        else:
            raise IOError(f"Video pre-processing failed on file {args.input}")
    else:
        raise IOError(f"Video pre-processing failed on file {args.input}")


def __align_timestamps(video, args, app_config):
    """
    reencode a video file with ffmpeg to align timestamps for whisker extraction.
    :param video:
    :param args:
    :param app_config:
    :return:
    """
    name, ext = path.splitext(video)
    aligned = name + "-aligned" + ext
    if not (path.exists(aligned) and KEEP_FILES):
        info(f'aligning timestamps and creating {aligned}')
        command = [app_config.system.ffmpeg_path, '-i', args.input, '-codec:v', 'mpeg4', '-r', '240',
                   '-qscale:v', '2', '-codec:a', 'copy', aligned]
        # todo replace with pexpect to anticipate overwrites ?
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return aligned if result.returncode == 0 else None
    else:
        info(f'found previously aligned timestamps for {aligned}')
        return aligned


def __check_requirements():
    """
    This code relies on a variety of external tools to be available.  If they aren't, warn and barf.
    :return: diagnostic information about what's missing.
    """
    system = platform.system().casefold()
    if system == "windows":
        pass
    else:
        error(f"{system} is not supported (windows only for now)")
        sys.exit(1)


def __validate_args(args):
    """
    Makes sure the arguments passed in are reasonable.  Sets the default value for output, if required.  Configures
    logging level.
    :param args:
    :return:
    """
    if not path.isfile(args.input):
        raise FileNotFoundError(f'{args.input} not found!')
    else:
        if not args.output:
            args.output = path.split(args.input)[0]
    if not access(args.output, W_OK):
        raise PermissionError(f'{args.output} is not writable!')
    if not args.verbose:
        getLogger().setLevel(ERROR)
    if args.config and not path.isfile(args.config):
        raise FileNotFoundError(f'User-supplied configuration file {args.config} not found!')


def __touch(fname, times=None):
    """ As coreutils touch.
    """
    with open(fname, 'a+'):
        utime(fname, times)


if __name__ == "__main__":
    # guarantee that we pass the right number of arguments.
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else ""))
