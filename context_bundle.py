"""Read bounded, explicitly scoped source snapshots and Git changes."""
import hashlib
import ast
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from windows_job import Job
from volatile_cache import SOURCE_CACHE

LOCAL=threading.local()


@contextmanager
def call_budget(timeout,cancelled=None,use_cache=True,wide_context=False):
    previous=(getattr(LOCAL,'deadline',None),getattr(LOCAL,'cancelled',None),getattr(LOCAL,'use_cache',True),getattr(LOCAL,'wide_context',False))
    LOCAL.deadline=time.monotonic()+timeout; LOCAL.cancelled=cancelled; LOCAL.use_cache=use_cache; LOCAL.wide_context=wide_context
    try: yield
    finally: LOCAL.deadline,LOCAL.cancelled,LOCAL.use_cache,LOCAL.wide_context=previous


def budget_remaining(default=15):
    event=getattr(LOCAL,'cancelled',None)
    if event is not None and event.is_set(): raise RuntimeError('Context/model call cancelled.')
    deadline=getattr(LOCAL,'deadline',None)
    if deadline is None: return default
    remaining=deadline-time.monotonic()
    if remaining<=0: raise TimeoutError('Total workflow deadline exceeded.')
    return min(default,remaining)

ROOT = Path(__file__).resolve().parent
MAX_FILES = 24
MAX_FILE_BYTES = 1_000_000
MAX_BUNDLE_CHARS = 70000
BLOCKED_DIRS = {'.git', '.codex', '.claude', '.aws', '.ssh', '.venv', 'venv',
                'node_modules', '__pycache__', '.cache'}
BLOCKED_NAMES = {'auth.json', 'credentials.json', 'credentials', '.netrc',
                 'id_rsa', 'id_ed25519', 'settings.local.json'}
TEXT_SUFFIXES = {'.py','.pyi','.js','.jsx','.ts','.tsx','.mjs','.cjs','.json','.json5',
    '.yaml','.yml','.toml','.ini','.cfg','.md','.txt','.log','.csv','.xml','.html','.css',
    '.scss','.sql','.sh','.ps1','.bat','.cmd','.c','.h','.cpp','.hpp','.cs','.java','.kt',
    '.rs','.go','.rb','.php','.vue','.svelte','.ets','.dart','.v','.sv','.tex','.r','.m'}


def allowed_roots():
    configured = os.environ.get('CLAUDE_MCP_ALLOWED_ROOTS')
    roots = json.loads(configured) if configured else [str(ROOT.parent)]
    if not isinstance(roots,list) or not roots or any(not isinstance(r,str) for r in roots):
        raise ValueError('CLAUDE_MCP_ALLOWED_ROOTS must be a JSON array of absolute directories.')
    if any(not Path(r).is_absolute() for r in roots):
        raise ValueError('Allowed roots must be absolute paths.')
    return [Path(r).resolve(strict=True) for r in roots]


def contained(path, root):
    return path == root or path.is_relative_to(root)


def project_path(value):
    if not isinstance(value,str) or not value or not Path(value).is_absolute():
        raise ValueError('project_root must be an absolute directory path.')
    path = Path(value).resolve(strict=True)
    if any(part.lower() in BLOCKED_DIRS for part in path.parts):
        raise ValueError('Sensitive/dependency directories cannot be project_root.')
    if not path.is_dir() or not any(contained(path,r) for r in allowed_roots()):
        raise ValueError('project_root is outside CLAUDE_MCP_ALLOWED_ROOTS or is not a directory.')
    return path


def source_path(project, relative):
    if not isinstance(relative,str) or not relative or len(relative)>500:
        raise ValueError('File paths must be non-empty relative paths, at most 500 characters.')
    win = PureWindowsPath(relative)
    if Path(relative).is_absolute() or win.drive or any(p=='..' or ':' in p for p in win.parts):
        raise ValueError('Absolute paths, parent traversal, and alternate data streams are prohibited.')
    parts = [p.lower() for p in win.parts]
    name = parts[-1]
    if any(p in BLOCKED_DIRS for p in parts) or name in BLOCKED_NAMES or name.startswith('.env'):
        raise ValueError('Credential/configuration/dependency path excluded from automatic context.')
    if win.suffix.lower() not in TEXT_SUFFIXES and name not in {'dockerfile','makefile','cmakelists.txt'}:
        raise ValueError('Only supported source/text files can be collected.')
    candidate = (project / Path(*win.parts)).resolve(strict=False)
    if not contained(candidate,project):
        raise ValueError('Resolved file path escapes project_root (including symlinks/junctions).')
    return candidate


def scrub(text, sanitizer=lambda text:text):
    text = sanitizer(text)
    text = re.sub(r'(?i)(\b(?:api[_-]?key|auth[_-]?token|access[_-]?token|password|secret)\b[\"\']?\s*[=:]\s*[\"\'])([^\"\'\r\n]{8,})([\"\'])', r'\1<redacted>\3', text)
    text = re.sub(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}',r'\1<redacted>',text)
    return re.sub(r'\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}', '<redacted>', text)


def decode(raw):
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):
        return raw.decode('utf-16')
    if b'\0' in raw:
        raise ValueError('Binary data is excluded.')
    return raw.decode('utf-8-sig')


def read_bytes(path, limit=MAX_FILE_BYTES):
    budget_remaining()
    if not path.is_file():
        raise ValueError('Not a regular file.')
    with path.open('rb') as handle:
        raw = handle.read(limit+1)
    if len(raw)>limit:
        raise ValueError(f'File exceeds {limit} bytes; supply a smaller explicit excerpt.')
    return raw


def excerpt(text, focus=None, cap=10000, tail=False):
    lines = text.splitlines()
    if focus:
        selected=set()
        for start,count in focus:
            if count:
                selected.update(range(max(1,start-15), min(len(lines),start+count+15)+1))
        numbers=sorted(selected) if selected else list(range(1,len(lines)+1))
    else:
        numbers=list(range(1,len(lines)+1))
    if tail: numbers.reverse()
    picked=[]; size=0
    for number in numbers:
        line=lines[number-1]
        if len(line)>2000: line=line[:2000]+' [line truncated]'
        size+=len(line)+20
        if size>cap: break
        picked.append((number,line))
    picked.sort()
    ranges=[]
    for number,_ in picked:
        if ranges and number==ranges[-1][1]+1: ranges[-1][1]=number
        else: ranges.append([number,number])
    return {'text':'\n'.join(f'{number}: {line}' for number,line in picked),
            'line_ranges':ranges,'total_lines':len(lines),
            'truncated':len(picked)<len(lines) or any(len(lines[n-1])>2000 for n,_ in picked)}


def snapshot(project, relative, raw=None, side='after', focus=None, sanitizer=lambda text:text):
    path=source_path(project,relative)
    raw=read_bytes(path) if raw is None else raw
    digest=hashlib.sha256(raw).hexdigest()
    redaction_key=getattr(sanitizer,'cache_key',None) if getattr(LOCAL,'use_cache',True) else None
    cap=40000 if getattr(LOCAL,'wide_context',False) else 10000
    key=(str(path),digest,side,json.dumps(focus),redaction_key,cap)
    hit=SOURCE_CACHE.get(key) if redaction_key is not None else None
    if hit: selected=hit[0]
    else:
        text=scrub(decode(raw),sanitizer); selected=excerpt(text,focus,cap=cap)
        if redaction_key is not None: SOURCE_CACHE.put(key,selected)
    return {'path':Path(relative).as_posix(),'side':side,'sha256':digest,
            'bytes':len(raw), **selected}


def git(project,args,timeout=15,missing_ok=False):
    binary=shutil.which('git.exe') or shutil.which('git')
    if not binary: raise ValueError('Git is not installed.')
    env=dict(os.environ)
    env['GIT_OPTIONAL_LOCKS']='0'; env['GIT_NO_REPLACE_OBJECTS']='1'
    for key in list(env):
        if key.startswith('GIT_') and key not in {'GIT_OPTIONAL_LOCKS','GIT_NO_REPLACE_OBJECTS'}:
            env.pop(key)
    timeout=budget_remaining(timeout)
    with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
        job=Job(); process=None
        try:
            process=subprocess.Popen([binary,'-c','core.fsmonitor=false','--literal-pathspecs','--no-pager','-C',str(project),*args],
                stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,shell=False)
            job.assign(process)
            deadline=time.monotonic()+timeout
            while process.poll() is None:
                budget_remaining()
                remaining=deadline-time.monotonic()
                if remaining<=0: raise TimeoutError('Git context command exceeded its deadline.')
                try: process.wait(timeout=min(.2,remaining))
                except subprocess.TimeoutExpired: pass
            job.close()
            if process.returncode==1 and missing_ok: return None
            if process.returncode:
                raise ValueError('Git could not collect this revision/change scope; use files mode for a non-Git project.')
            stdout.seek(0); data=stdout.read(2_000_001)
            if len(data)>2_000_000: raise ValueError('Git context exceeds capture limit; narrow the selected scope.')
            return data
        finally:
            job.close()
            if process is not None:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)


def files_argument(value, required=False):
    if value is None and not required: return None
    if not isinstance(value,list) or not value or len(value)>MAX_FILES or any(not isinstance(s,str) for s in value):
        raise ValueError(f'files must contain 1–{MAX_FILES} explicit relative file paths.')
    return list(dict.fromkeys(value))


def base_revision(project,value):
    if not isinstance(value,str) or not value or len(value)>160 or value.startswith('-') or any(c.isspace() for c in value):
        raise ValueError('Invalid base_ref.')
    value_bytes=git(project,['rev-parse','--verify','--quiet','--end-of-options',value+'^{commit}'],missing_ok=value=='HEAD')
    if value_bytes is None:
        # HEAD exists symbolically but its branch has no commit yet.
        branch=git(project,['symbolic-ref','--quiet','HEAD']).decode().strip()
        if git(project,['show-ref','--verify','--quiet',branch],missing_ok=True) is None: return None
        raise ValueError('HEAD could not be resolved.')
    return value_bytes.decode().strip()


def git_paths(project,scope,base):
    if scope=='staged': args=['diff','--cached','--name-only','-z','--no-renames',base]
    elif scope=='unstaged': args=['diff','--name-only','-z','--no-renames']
    else: args=['diff','--name-only','-z','--no-renames',base]
    paths=git(project,args).decode('utf-8').split('\0')
    if scope=='working_tree':
        paths+=git(project,['ls-files','--others','--exclude-standard','-z']).decode('utf-8').split('\0')
    return list(dict.fromkeys(p for p in paths if p))


def collect_review(arguments,sanitizer=lambda text:text):
    project=project_path(arguments.get('project_root'))
    scope=arguments.get('scope','working_tree')
    if scope not in {'working_tree','staged','unstaged','files'}:
        raise ValueError('scope must be working_tree, staged, unstaged, or files.')
    supplied=files_argument(arguments.get('files'),required=scope=='files')
    bundle={'project_root':str(project),'scope':scope,'base_commit':None,
            'snapshots':[],'diffs':[],'skipped':[],'warnings':[],'absent_after':[],
            'reviewed_paths':[],'related_paths':[],'has_changes':True}
    if scope=='files':
        paths=supplied; repo=project
    else:
        repo=Path(git(project,['rev-parse','--show-toplevel']).decode('utf-8').strip()).resolve()
        if not any(contained(repo,r) for r in allowed_roots()):
            raise ValueError('Git repository root is outside allowed roots.')
        bundle['requested_project_root']=str(project)
        bundle['project_root']=str(repo)
        base=base_revision(repo,arguments.get('base_ref','HEAD') if scope=='working_tree' else 'HEAD')
        bundle['base_commit']=base
        baseline=base or git(repo,['hash-object','-t','tree','--stdin']).decode().strip()
        bundle['base_tree']=baseline if base is None else None
        paths=git_paths(repo,scope,baseline)
        prefix=project.relative_to(repo)
        paths=[p for p in paths if contained((repo/p).resolve(strict=False),project)]
        if supplied:
            names={str((prefix/Path(*PureWindowsPath(p).parts)).as_posix()) for p in supplied}
            paths=[p for p in paths if p in names]
        bundle['has_changes']=bool(paths)
    for index,relative in enumerate(paths):
        budget_remaining()
        if index>=MAX_FILES:
            bundle['skipped'].append({'path':relative,'reason':'file-count limit'}); continue
        try:
            path=source_path(repo,relative)
            if scope=='files':
                entries=[snapshot(repo,relative,sanitizer=sanitizer)]; diff=None
            else:
                args=['diff','--no-ext-diff','--no-textconv','--no-renames','--unified=5']
                if scope=='staged': args+=['--cached',baseline]
                elif scope=='working_tree': args+=[baseline]
                diff=scrub(git(repo,[*args,'--',relative]).decode('utf-8'),sanitizer)
                if scope=='staged':
                    try: current=git(repo,['show',':'+relative])
                    except ValueError: current=None
                else: current=read_bytes(path) if path.exists() else None
                before=None
                try: before=git(repo,['show',(':'+relative) if scope=='unstaged' else baseline+':'+relative])
                except ValueError: pass
                entries=[]
                for side,raw,group in [('after',current,3),('before',before,1)]:
                    if raw is not None:
                        if len(raw)>MAX_FILE_BYTES: raise ValueError('File exceeds byte limit.')
                        focus=[(int(m[group]),int(m[group+1] or '1')) for m in re.finditer(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',diff,re.M)]
                        entries.append(snapshot(repo,relative,raw,side,focus,sanitizer))
                if not diff and current is not None:
                    diff='[Untracked file: the after snapshot contains its content.]'
            if not entries: raise ValueError('No readable source snapshot available.')
            item={'path':Path(relative).as_posix(),'text':diff[:12000],'truncated':len(diff)>12000} if diff else None
            tentative={**bundle,'snapshots':bundle['snapshots']+entries,'diffs':bundle['diffs']+([item] if item else [])}
            if len(json.dumps(tentative,ensure_ascii=False))>(200000 if getattr(LOCAL,'wide_context',False) else MAX_BUNDLE_CHARS):
                raise ValueError('Context budget exhausted; narrow files or change scope.')
            bundle['snapshots']+=entries
            bundle['reviewed_paths'].append(Path(relative).as_posix())
            if item: bundle['diffs'].append(item)
            if scope!='files' and not any(s['side']=='after' for s in entries):
                bundle['absent_after'].append(Path(relative).as_posix())
        except (ValueError,OSError,UnicodeError) as exc:
            bundle['skipped'].append({'path':relative,'reason':str(exc)})
    if bundle['skipped'] or any(s['truncated'] for s in bundle['snapshots']) or any(d['truncated'] for d in bundle['diffs']):
        bundle['warnings'].append('Coverage is partial; inspect skipped entries and excerpt ranges before accepting the review.')
    if not paths: bundle['warnings'].append('No changes in selected scope.')
    if arguments.get('include_related',True) and bundle['snapshots']:
        add_related(bundle,repo,project,sanitizer)
    return bundle


def related_candidates(project,relative,text):
    source=project/relative; candidates=[]
    if source.suffix=='.py':
        try:
            tree=ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):
                    for item in node.names: candidates.append(project/Path(*item.name.split('.')))
                elif isinstance(node,ast.ImportFrom):
                    parent=source.parent if node.level else project
                    for _ in range(max(0,node.level-1)): parent=parent.parent
                    if node.module: candidates.append(parent/Path(*node.module.split('.')))
                    else:
                        candidates.extend(parent/item.name for item in node.names)
        except SyntaxError: pass
    elif source.suffix in {'.js','.jsx','.ts','.tsx','.mjs','.cjs'}:
        for name in re.findall(r'(?:from\s*|require\s*\(\s*|import\s*\(\s*|import\s*)[\"\'](\.{1,2}/[^\"\']+)',text):
            candidates.append(source.parent/name)
    stem=source.stem
    if not stem.startswith('test_') and not stem.endswith('_test'):
        candidates.extend([source.parent/f'test_{stem}.py',project/'tests'/f'test_{stem}.py',
                           source.parent/f'{stem}.test.ts',source.parent/f'{stem}.test.js'])
    for candidate in candidates:
        variants=[candidate] if candidate.suffix else [candidate.with_suffix(s) for s in ('.py','.ts','.tsx','.js','.jsx')]+[candidate/'__init__.py',candidate/'index.ts']
        for variant in variants:
            canonical=variant.resolve(strict=False)
            if contained(canonical,project) and canonical.is_file():
                yield canonical.relative_to(project).as_posix()


def add_related(bundle,repo,requested_project,sanitizer):
    seen=set(bundle['reviewed_paths']); count=0
    primary=[s for s in bundle['snapshots'] if s['side']=='after']
    for item in primary:
        try:
            raw=git(repo,['show',':'+item['path']]) if bundle['scope']=='staged' else read_bytes(source_path(repo,item['path']))
            if hashlib.sha256(raw).hexdigest()!=item['sha256']: continue # staged/worktree differ
            names=related_candidates(repo,item['path'],decode(raw))
            for name in names:
                if name in seen or count>=4 or len(seen)>=MAX_FILES: continue
                path=source_path(repo,name)
                if not contained(path,requested_project): continue
                extra=snapshot(repo,name,sanitizer=sanitizer); extra['role']='related'
                tentative={**bundle,'snapshots':bundle['snapshots']+[extra]}
                if len(json.dumps(tentative,ensure_ascii=False))>(200000 if getattr(LOCAL,'wide_context',False) else MAX_BUNDLE_CHARS): continue
                bundle['snapshots'].append(extra); bundle['related_paths'].append(name); seen.add(name); count+=1
                if extra['truncated']:
                    bundle['warnings'].append(f'Related source excerpt truncated: {name}')
        except (ValueError,OSError,UnicodeError): continue


def collect_failure(arguments,sanitizer=lambda text:text):
    project=project_path(arguments.get('project_root'))
    files=files_argument(arguments.get('files'),required=True)
    inline=arguments.get('log','')
    logfile=arguments.get('log_file')
    if not isinstance(inline,str) or len(inline)>50000 or (not inline.strip() and not logfile):
        raise ValueError('Provide log text (at most 50000 chars) or a project-relative log_file.')
    if logfile:
        path=source_path(project,logfile)
        raw=read_bytes(path)
        file_text=decode(raw)
        log_sha=hashlib.sha256(raw).hexdigest()
    else: file_text=''; log_sha=None
    log=scrub(inline+'\n'+file_text,sanitizer)
    bundle=collect_review({'project_root':str(project),'scope':'files','files':files,
                           'include_related':arguments.get('include_related',True)},sanitizer)
    bundle['failure_log']={'path':logfile,'sha256':log_sha,'tail':log[-15000:], 'truncated':len(log)>15000}
    if len(log)>15000: bundle['warnings'].append('Failure log truncated to last 15000 characters.')
    if len(json.dumps(bundle,ensure_ascii=False))>(215000 if getattr(LOCAL,'wide_context',False) else 85000):
        raise ValueError('Diagnostic context exceeds budget; reduce source files or log size.')
    return bundle


def verify_snapshots(bundle):
    project=Path(bundle['project_root'])
    states={'changed':[],'unreadable':[],'verification_timeout':[],'verification_cancelled':[]}
    for item in bundle['snapshots']:
        if item['side']!='after': continue
        # staged snapshots describe the index, not the working file.
        try:
            if bundle['scope']=='staged' and item.get('role')!='related': raw=git(project,['show',':'+item['path']])
            else:
                path=source_path(project,item['path'])
                if not path.exists(): states['changed'].append(item['path']); continue
                raw=read_bytes(path)
            if hashlib.sha256(raw).hexdigest()!=item['sha256']: states['changed'].append(item['path'])
        except TimeoutError: states['verification_timeout'].append(item['path'])
        except RuntimeError:
            event=getattr(LOCAL,'cancelled',None)
            if event is None or not event.is_set(): raise
            states['verification_cancelled'].append(item['path'])
        except (ValueError,OSError) as exc: states['unreadable'].append({'path':item['path'],'reason':str(exc)})
    for relative in bundle.get('absent_after',[]):
        if bundle['scope']=='staged':
            try: git(project,['show',':'+relative]); states['changed'].append(relative)
            except TimeoutError: states['verification_timeout'].append(relative)
            except RuntimeError:
                event=getattr(LOCAL,'cancelled',None)
                if event is None or not event.is_set(): raise
                states['verification_cancelled'].append(relative)
            except ValueError: pass
        else:
            try:
                if source_path(project,relative).exists(): states['changed'].append(relative)
            except TimeoutError: states['verification_timeout'].append(relative)
            except (ValueError,OSError) as exc: states['unreadable'].append({'path':relative,'reason':str(exc)})
    states['changed']=sorted(set(states['changed']))
    states['verification_timeout']=sorted(set(states['verification_timeout']))
    states['verification_cancelled']=sorted(set(states['verification_cancelled']))
    return states


def stale_snapshots(bundle):
    return verify_snapshots(bundle)['changed']
