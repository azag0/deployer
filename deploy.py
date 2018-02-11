from pathlib import Path
import hashlib
import sys
import subprocess as sp
import os
import tarfile
from tempfile import NamedTemporaryFile
from argparse import ArgumentParser
from string import Template


def git_rsync(src, tgt, flags):
    src = str(src)
    tgt = str(tgt)
    dirs = set()
    files = []
    for path in sp.check_output(['git', 'ls-files'], cwd=src).split():
        files.append(path)
        dirs.update(Path(path.decode()).parents)
    files.extend(f'{path}\n'.encode() for path in dirs)
    args = ['rsync', *flags, src + '/', tgt + '/']
    sp.run(args, input=b'\n'.join(files), check=True)


def save_diff(diffdir, name, diff):
    with NamedTemporaryFile('w') as f:
        f.write(diff)
        f.flush()
        archive = f'{name}.diff.tar.gz'
        with tarfile.open(str(diffdir/archive), 'w|gz') as archfile:
            archfile.add(f.name, 'diff')
    print('Saved diff to {}.'.format(archive))


def notify(title, msg):
    sp.check_call([
        'reattach-to-user-namespace',
        'terminal-notifier', '-message', msg, '-title', title
    ])


def get_diff(mainline):
    diff = sp.check_output(['git', 'diff', mainline])
    difftext = f'master: {mainline}\n'
    if diff.strip():
        difftext += str(diff)
        sha = hashlib.sha1(difftext.encode()).hexdigest()
        return sha, difftext
    else:
        return mainline, difftext


def deploy(conf, host=None, cmd=None, profile=None, dry=False):
    name = conf.name
    top = conf.top
    cmd = cmd or conf.cmd
    dest = Path(conf.dest)
    rsync_flags = [
        '-ai',
        '--delete-excluded',
        '--include-from=-',
        *('--include=' + patt for patt in getattr(conf, 'include', [])),
        *('--filter=P ' + patt for patt in getattr(conf, 'exclude', [])),
        '--exclude=*'
    ]
    if dry:
        rsync_flags.append('-n')
    branch = sp.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).decode().strip()
    mainline = sp.check_output(['git', 'merge-base', 'HEAD', 'origin/master']).decode().strip()
    if profile:
        branch = f'{branch}:{profile}'
    sha, diff = get_diff(mainline)
    mainline, sha = mainline[:7], sha[:7]
    if sha != mainline:
        print(f'Got diff {sha} of {branch} with respect to {mainline} (master).')
    else:
        print(f'On mainline {mainline} (master).')
    save_diff(Path(conf.diffdir).expanduser(), f'{name}-{sha}', diff)
    prefix = f'branches/{branch}'
    if host:
        sp.check_call(['ssh', host, ':'])
        print(f'Syncing {branch} to {host}...')
        if hasattr(conf, 'presync'):
            conf.presync(lambda src, tgt: git_rsync(src, f'{host}:{tgt}', rsync_flags))
        sp.check_call(['ssh', host, f'mkdir -p {dest/prefix}'])
        git_rsync('.', f'{host}:{dest/prefix/top}', rsync_flags)
    else:
        dest = dest.expanduser()
        (dest/prefix).mkdir(exist_ok=True)
        git_rsync('.', dest/prefix/top, rsync_flags)
    if dry:
        return
    build_script = ['set -e']
    if hasattr(conf, 'prebuild'):
        build_script.append(conf.prebuild)
    build_script.extend([
        f"echo 'Building with `{cmd}`...'",
        f'pushd {dest/prefix/top}',
        cmd,
        f'pushd {dest}',
    ])
    if hasattr(conf, 'postbuild'):
        build_script.append(conf.postbuild)
    build_script = Template('\n'.join(build_script)).safe_substitute({
        'PREFIX': prefix,
        'SHA': sha,
        'TOP': top,
        'BRANCH': branch,
    })
    msg = f'Finished compiling {name} at {sha}'
    if host:
        print(f'Connecting to {host} to make {sha} from {branch}...')
        script_file = sp.run(
            ['ssh', host, 'F=$(mktemp) && cat >$F && echo $F'],
            check=True,
            input=build_script.encode(),
            stdout=sp.PIPE
        ).stdout.decode()
        sp.run(['ssh', '-t', host, f'cd {dest} && bash {script_file}'], check=True)
        msg += f' on {host}'
    else:
        sp.check_call(build_script, shell=True)
    notify(name, msg)


def main(change_dir=None, only_build=False, **kwargs):
    if change_dir:
        os.chdir(change_dir)
    sys.path.append('.')
    import deploy_conf as conf
    deploy(conf, **kwargs)


def parse_cli():
    parser = ArgumentParser(add_help=False)
    arg = parser.add_argument
    arg('-C', metavar='DIR', dest='change_dir')
    arg('-h', '--host')
    arg('-p', '--profile')
    arg('-n', '--dry', action='store_true')
    arg('cmd', nargs='?', metavar='CMD')
    return vars(parser.parse_args())


if __name__ == '__main__':
    main(**parse_cli())
