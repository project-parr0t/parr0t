"""Profile I/O and core pack binding with generated synthetic inputs only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.unity_6000_4_fixtures import bundle, text_file
import unity_adapter as sdk


class BoundaryTests(unittest.TestCase):
    def test_relative_path_escape_and_absolute_paths_rejected(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-boundary-') as directory:
            for value in ['../escape','C:/synthetic','/synthetic','synthetic\\file']:
                with self.subTest(value=value),self.assertRaises(ValueError): sdk.relative(directory,value)

    def test_result_coverage_status_source_and_tokens_are_exact(self):
        plan={'files':[{'bindings':[{'id':'synthetic-1','source':'SYNTHETIC_{0}\n'}]}]}
        good={'segment_id':'synthetic-1','source':'SYNTHETIC_{0}\n','text':'SYNTHETIC_TARGET_{0}\n','status':'reviewed'}
        with tempfile.TemporaryDirectory(prefix='synthetic-results-') as directory:
            path=Path(directory)/'results.json'
            for rows in [[],[good,good],[dict(good,segment_id='synthetic-missing')],
                [dict(good,status='translated')],[dict(good,source='SYNTHETIC_STALE')],
                [dict(good,text='SYNTHETIC_TARGET')],[dict(good,status='source_preserved')]]:
                path.write_text(json.dumps({'results':rows}),encoding='utf-8')
                with self.subTest(rows=rows),self.assertRaises(ValueError): sdk.translations(plan,path)
            path.write_text(json.dumps({'results':[good]}),encoding='utf-8')
            self.assertEqual(sdk.translations(plan,path),{'synthetic-1':good['text']})


@unittest.skipUnless(importlib.util.find_spec('UnityPy') is not None,'Install pinned private UnityPy')
class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='synthetic-profile-')
        self.root=Path(self.temp.name)
        self.source,self.workspace=self.root/'source',self.root/'workspace'
        self.source.mkdir();self.workspace.mkdir()
        raw,_=text_file()
        self.raw=bundle({'synthetic-member':raw,'synthetic-stream':b'SYNTHETIC_OPAQUE'})
        (self.source/'synthetic-container.bin').write_bytes(self.raw)
        self.binding=dict(id='synthetic-1',member='synthetic-member',object_id=11,
            field_path=['m_Label'],source='SYNTHETIC_SOURCE_{0}')
        self.plan=dict(schema_version=1,engine='unity',engine_build='6000.4.0f1',backend='Mono',
            game_root=str(self.source),files=[dict(path='synthetic-container.bin',kind='unityfs',
                sha256=sdk.digest(self.raw),bindings=[self.binding])])
        self.plan_path=self.workspace/'bindings.json'
        sdk.write_json(self.plan_path,self.plan)
        self.environment=patch.dict(os.environ,{'GAME_L10N_WORKDIR':str(self.workspace),
            'GAME_L10N_BINDINGS':str(self.plan_path),'GAME_L10N_STAGE':'pack'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop();self.temp.cleanup()

    def results(self):
        path=self.workspace/'results.json'
        sdk.write_json(path,{'results':[dict(segment_id='synthetic-1',source=self.binding['source'],
            text='SYNTHETIC_TARGET_{0}',status='reviewed')]})
        return path

    def test_inventory_validates_bindings_but_declares_incomplete_coverage(self):
        output=self.workspace/'inventory'
        summary=sdk.extract_inventory(self.source,output)
        self.assertEqual(summary['segments'],1)
        self.assertFalse(summary['static_complete'])
        self.assertFalse(sdk.read_json(output/'coverage.json')['runtime_verified'])
        self.assertEqual(sdk.read_json(output/'segments.json')['segments'][0]['source'],self.binding['source'])
        self.assertEqual((self.source/'synthetic-container.bin').read_bytes(),self.raw)

    def test_pack_records_only_current_core_bound_results(self):
        from game_l10n.coverage import COMPLETION_SCHEMA
        from game_l10n.tooling import cached_tool_state
        path=self.results()
        control=self.workspace/'.game-l10n'
        control.mkdir()
        sdk.write_json(control/'completion-attestation.json',dict(schema=COMPLETION_SCHEMA,
            target_completion=True,static_completion=True,result_artifact=dict(path=str(path.resolve()),
            size=path.stat().st_size,sha256=sdk.digest(path.read_bytes()))))
        output=self.workspace/'payload'
        self.assertEqual(sdk.build_payload(path,output)['status'],'verified')
        self.assertEqual(cached_tool_state(self.workspace,'pack')['status'],'valid')
        from unity_serialized import bundle_members,environment
        _,members=bundle_members((output/'synthetic-container.bin').read_bytes())
        self.assertEqual(environment(members['synthetic-member']).file.objects[11].read_typetree()['m_Label'],
                         'SYNTHETIC_TARGET_{0}')
        self.assertEqual((self.source/'synthetic-container.bin').read_bytes(),self.raw)
        path.write_text(path.read_text()+' ',encoding='utf-8')
        self.assertEqual(cached_tool_state(self.workspace,'pack')['status'],'stale')

    def test_missing_completion_cannot_create_valid_pack_state(self):
        from game_l10n.errors import ToolExecutionError
        from game_l10n.tooling import cached_tool_state
        output=self.workspace/'held-payload'
        with patch.object(sdk,'configuration',side_effect=AssertionError('SOURCE_PROCESSING_REACHED')) as configure:
            with self.assertRaises(ToolExecutionError): sdk.build_payload(self.results(),output)
            configure.assert_not_called()
        self.assertEqual(cached_tool_state(self.workspace,'pack')['status'],'missing')
        self.assertFalse(output.exists())

    def test_stale_completion_rejects_before_payload_creation(self):
        from game_l10n.coverage import COMPLETION_SCHEMA
        from game_l10n.errors import ToolExecutionError
        path=self.results()
        control=self.workspace/'.game-l10n'
        control.mkdir()
        sdk.write_json(control/'completion-attestation.json',dict(schema=COMPLETION_SCHEMA,
            target_completion=True,static_completion=True,result_artifact=dict(path=str(path.resolve()),
            size=path.stat().st_size,sha256='0'*64)))
        output=self.workspace/'stale-payload'
        with patch.object(sdk,'configuration',side_effect=AssertionError('SOURCE_PROCESSING_REACHED')) as configure:
            with self.assertRaises(ToolExecutionError): sdk.build_payload(path,output)
            configure.assert_not_called()
        self.assertFalse(output.exists())

    def test_repository_output_and_ancestor_workspace_are_rejected(self):
        repository=self.workspace/'synthetic-repository'
        fake_module=repository/'profiles/sdk-unity-6000.4/tools/unity_adapter.py'
        with patch.object(sdk,'__file__',str(fake_module)):
            for output in [repository/'payload',self.workspace/'outside-repository-payload']:
                with self.subTest(output=output),self.assertRaisesRegex(ValueError,'SOURCE_WORKSPACE_SEPARATION_REQUIRED'):
                    sdk.configuration(self.source,output)

    def test_stale_source_ambiguous_ids_wrong_backend_and_source_output_fail(self):
        variants=[dict(self.plan,backend='IL2CPP'),dict(self.plan,engine_build='2022.3.0f1')]
        stale=copy.deepcopy(self.plan);stale['files'][0]['sha256']='0'*64
        duplicate=copy.deepcopy(self.plan);duplicate['files'][0]['bindings']*=2
        for plan in variants+[stale,duplicate]:
            sdk.write_json(self.plan_path,plan)
            with self.subTest(plan=plan),self.assertRaises(ValueError):
                sdk.extract_inventory(self.source,self.workspace/'invalid-output')
        sdk.write_json(self.plan_path,self.plan)
        with self.assertRaises(ValueError): sdk.extract_inventory(self.source,self.source/'output')
