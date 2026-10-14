"""Generate a local Codex MCP configuration; install only with explicit flags."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib


def config_fragment(project,roots,exe):
    quote=lambda value: json.dumps(str(value),ensure_ascii=False)
    return '\n'.join([
        '[mcp_servers.claude_cli]', 'command = '+quote(sys.executable),
        'args = ['+quote(project/'bridge.py')+']', 'cwd = '+quote(project),
        'startup_timeout_sec = 10', 'tool_timeout_sec = 660', 'enabled = true',
        '[mcp_servers.claude_cli.env]', 'PYTHONIOENCODING = "utf-8"',
        'CLAUDE_MCP_EXE = '+quote(exe),
        'CLAUDE_MCP_ALLOWED_ROOTS = '+quote(json.dumps([str(p) for p in roots],ensure_ascii=False)), ''])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allowed-root',action='append',required=True,help='Existing absolute project directory; repeatable.')
    parser.add_argument('--claude-exe',help='Absolute native Claude executable path.')
    parser.add_argument('--config',type=Path,help='Target config.toml (defaults to CODEX_HOME/config.toml).')
    parser.add_argument('--install',action='store_true',help='Append claude_cli only if it is absent; back up the existing config.')
    parser.add_argument('--install-skill',action='store_true',help='Copy the bundled skill only if its destination is absent.')
    options=parser.parse_args()
    project=Path(__file__).resolve().parent
    codex_home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    config=(options.config or codex_home/'config.toml').resolve()
    roots=[]
    for value in options.allowed_root:
        path=Path(value)
        if not path.is_absolute(): parser.error('--allowed-root must be absolute.')
        path=path.resolve(strict=True)
        if not path.is_dir(): parser.error('--allowed-root must be a directory.')
        roots.append(path)
    candidate=options.claude_exe or shutil.which('claude.exe')
    if not candidate:
        candidate=str(Path(os.environ.get('APPDATA',''))/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe')
    exe=Path(candidate)
    if not exe.is_absolute() or not exe.is_file() or exe.suffix.lower()!='.exe':
        parser.error('Native claude.exe not found; supply --claude-exe.')
    fragment=config_fragment(project,roots,exe.resolve())
    tomllib.loads(fragment)
    old=config.read_text(encoding='utf-8-sig') if config.exists() else ''
    parsed=tomllib.loads(old)
    destination=codex_home/'skills/codex-claude-review'
    # Finish all preconditions before changing either destination.
    if options.install and 'claude_cli' in parsed.get('mcp_servers',{}):
        parser.error('claude_cli already exists; review the generated fragment and update manually.')
    if options.install_skill and destination.exists():
        parser.error('Skill destination already exists; refusing to overwrite it.')
    if options.install:
        combined=old.rstrip()+'\n\n'+fragment
        tomllib.loads(combined)
        config.parent.mkdir(parents=True,exist_ok=True)
        if config.exists():
            backup=config.with_name(config.name+'.before-claude-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.bak')
            shutil.copy2(config,backup)
        fd,temporary=tempfile.mkstemp(prefix='.claude-config-',dir=config.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as handle: handle.write(combined)
            os.replace(temporary,config)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print('Installed local MCP configuration.')
    if options.install_skill:
        shutil.copytree(project/'skill/codex-claude-review',destination)
        print('Installed bundled skill.')
    if not options.install: print(fragment)
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
