"""Compile only SDK tooling and a synthetic assembly in a temporary workspace."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.unity_6000_4_fixtures import TOOLS


CSC = os.environ.get('GAME_L10N_CSC','')
CECIL = os.environ.get('GAME_L10N_CECIL','')


@unittest.skipUnless(Path(CSC).is_file() and Path(CECIL).is_file(),
    'Set GAME_L10N_CSC and GAME_L10N_CECIL to the private C# compiler and Mono.Cecil 0.11.6')
class ManagedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='synthetic-managed-')
        cls.root = Path(cls.temp.name)
        shutil.copyfile(CECIL,cls.root/'Mono.Cecil.dll')
        cls.writer,cls.factory = cls.root/'Writer.exe',cls.root/'Factory.exe'
        for source,output in [(TOOLS/'ManagedLiteralWriter.cs',cls.writer),
            (Path(__file__).parent/'fixtures/unity_6000_4/ManagedFixture.cs',cls.factory)]:
            result = subprocess.run([CSC,'/nologo','/target:exe','/out:'+str(output),
                '/reference:'+str(cls.root/'Mono.Cecil.dll'),'/reference:System.Web.Extensions.dll',str(source)],
                capture_output=True,check=False)
            if result.returncode: raise AssertionError('SYNTHETIC_TOOL_COMPILE_FAILED: '+result.stdout.decode(errors='replace'))
        cls.source = cls.root/'SyntheticFixture.dll'
        subprocess.run([str(cls.factory),str(cls.source),str(cls.root/'binding.json')],capture_output=True,check=True)
        cls.binding=json.loads((cls.root/'binding.json').read_text(encoding='utf-8-sig'))

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def request(self, **changes):
        row = {k:v for k,v in self.binding.items() if k != 'protected_method'}
        row['target']='SYNTHETIC_TARGET_{0}'
        request=dict(schema_version=1,input=str(self.source),output=str(self.root/(self._testMethodName+'.dll')),
            sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            protected_methods=[self.binding['protected_method']],literals=[row])
        request.update(changes)
        return request

    def invoke(self, request):
        path=self.root/'request.json'
        path.write_text(json.dumps(request),encoding='utf-8')
        return subprocess.run([str(self.writer),str(path)],capture_output=True,check=False)

    def test_exact_literal_readback_preserves_types_fields_switch_resource_accessor_and_resource(self):
        before=self.source.read_bytes()
        request=self.request()
        result=self.invoke(request)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)['status'],'verified')
        self.assertTrue(Path(request['output']).is_file())
        self.assertEqual(self.source.read_bytes(),before)
        # The writer independently rereads every body, branch target, field/type
        # signature and embedded resource; a second stale source binding fails.
        second=dict(request,input=request['output'],output=str(self.root/'second.dll'),
                    sha256=hashlib.sha256(Path(request['output']).read_bytes()).hexdigest())
        self.assertIn(b'MANAGED_LITERAL_CHANGED',self.invoke(second).stderr)

    def test_stale_source_duplicate_sites_protected_methods_and_wrong_identity_fail(self):
        base=self.request()
        row=base['literals'][0]
        variants=[dict(base,sha256='0'*64),dict(base,literals=[row,row]),
            dict(base,protected_methods=[row['method_token']]),
            dict(base,literals=[dict(row,method_full_name='SYNTHETIC_STALE_METHOD')]),
            dict(base,literals=[dict(row,source='SYNTHETIC_STALE_SOURCE')])]
        codes=[b'MANAGED_SOURCE_CHANGED',b'MANAGED_SITE_AMBIGUOUS',b'MANAGED_METHOD_BINDING_INVALID',
               b'MANAGED_METHOD_CHANGED',b'MANAGED_LITERAL_CHANGED']
        for request,code in zip(variants,codes):
            with self.subTest(code=code):
                result=self.invoke(request)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(code,result.stderr)
                self.assertFalse(Path(request['output']).exists())

    def test_inspection_validates_bindings_without_writing_an_assembly(self):
        request=self.request(operation='inspect')
        result=self.invoke(request)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)['status'],'bound')
        self.assertFalse(Path(request['output']).exists())
