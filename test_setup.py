import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.config=self.root/'config.toml'; self.home=self.root/'codex-home'
        self.env={**os.environ,'CODEX_HOME':str(self.home),'PYTHONUTF8':'1'}
        self.script=Path(__file__).resolve().parent/'setup_mcp.py'
        self.args=[sys.executable,str(self.script),'--allowed-root',str(self.root),
                   '--claude-exe',sys.executable,'--config',str(self.config)]

    def tearDown(self): self.temp.cleanup()

    def run_setup(self,*extra):
        return subprocess.run([*self.args,*extra],env=self.env,capture_output=True,text=True,encoding='utf-8',timeout=15)

    def test_preview_changes_nothing(self):
        completed=self.run_setup(); self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertFalse(self.config.exists()); self.assertFalse(self.home.exists())
        server=tomllib.loads(completed.stdout)['mcp_servers']['claude_cli']
        self.assertEqual(json.loads(server['env']['CLAUDE_MCP_ALLOWED_ROOTS']),[str(self.root.resolve())])
        self.assertEqual(server['tool_timeout_sec'],660)

    def test_install_preserves_existing_configuration_and_backup(self):
        old='model = "example-model"\n[mcp_servers.existing]\ncommand = "example-command"\n'
        self.config.write_text(old,encoding='utf-8')
        completed=self.run_setup('--install'); self.assertEqual(completed.returncode,0,completed.stderr)
        current=tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(current['model'],'example-model'); self.assertIn('existing',current['mcp_servers'])
        backups=list(self.root.glob('*.bak')); self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'),old)
        before=self.config.read_bytes(); refused=self.run_setup('--install')
        self.assertNotEqual(refused.returncode,0); self.assertEqual(before,self.config.read_bytes())

    def test_skill_collision_prevents_config_mutation(self):
        destination=self.home/'skills/codex-claude-review'; destination.mkdir(parents=True)
        (destination/'keep.txt').write_text('existing',encoding='utf-8')
        refused=self.run_setup('--install','--install-skill')
        self.assertNotEqual(refused.returncode,0); self.assertFalse(self.config.exists())
        self.assertEqual((destination/'keep.txt').read_text(),'existing')


if __name__=='__main__': unittest.main()
