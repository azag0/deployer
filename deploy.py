from pathlib import Path
import hashlib
import sys
import subprocess as sp
import os
import tarfile
from tempfile import NamedTemporaryFile
from argparse import ArgumentParser
from functools import partial
from string import Template


class Context:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def git_rsync(repo, to, flags):
    dirs = set()
    files = []
    for path in sp.check_output(['git', 'ls-files'], cwd=repo).split():
        files.append(path)
        dirs.update(Path(path.decode()).parents)
    files.extend(f'{path}\n'.encode() for path in dirs)
    args = ['rsync', *flags, repo + '/', str(to)]
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


def get_diff(path, mainline):
    diff = sp.check_output(['git', 'diff', mainline], cwd=path)
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
    dest = Path('~/var/Builds')/conf.name
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
    branch = sp.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=top).decode().strip()
    mainline = sp.check_output(['git', 'merge-base', 'HEAD', 'origin/master'], cwd=top).decode().strip()
    if profile:
        branch = f'{branch}:{profile}'
    sha, diff = get_diff(top, mainline)
    mainline, sha = mainline[:7], sha[:7]
    if sha != mainline:
        print(f'Got diff {sha} of {branch} with respect to {mainline} (master).')
    else:
        print(f'On mainline {mainline} (master).')
    save_diff(Path.home()/'Archive/diffs', f'{name}-{sha}', diff)
    prefix = dest/'branches'/branch
    if host:
        sp.check_call(['ssh', host, ':'])
        print(f'Syncing {branch} to {host}...')
        if hasattr(conf, 'presync'):
            ctx = Context(host=host, dest=dest, git_sync=partial(git_rsync, flags=rsync_flags))
            conf.presync(ctx)
        sp.check_call(['ssh', host, f'mkdir -p {prefix}'])
        git_rsync(top, f'{host}:{prefix/top}', rsync_flags)
    else:
        dest = dest.expanduser()
        prefix = prefix.expanduser()
        prefix.mkdir(exist_ok=True)
        git_rsync(top, prefix/top, rsync_flags)
    if dry:
        return
    build_script = ['set -e']
    if hasattr(conf, 'prebuild'):
        build_script.append(conf.prebuild)
    build_script.extend([
        f"echo 'Building with `{cmd}`...'",
        f'pushd {prefix}',
        cmd,
        'popd',
    ])
    if hasattr(conf, 'postbuild'):
        build_script.append(conf.postbuild)
    build_script = Template('\n'.join(build_script)).safe_substitute({
        'DEST': dest,
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
