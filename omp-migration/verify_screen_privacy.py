"""Synthetic screenshots only: real local OCR, no desktop login or Discord posts."""
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from evo_omp import context, screen_privacy as privacy

BROWSER = '--browser' in sys.argv
if BROWSER:sys.argv.remove('--browser')


def fixture():
    image = Image.new('RGB', (960, 640), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 24)
    rows = ['user@example.test', 'password: Umbrella7426', 'otp: 123456',
            'ghp_abcdefghijkmnopqrstuvwxy23456789', 'PrivateValue78642', 'Normal report 2026']
    boxes = []
    for i, text in enumerate(rows):
        origin = (30, 30 + i*60)
        draw.text(origin, text, fill='black', font=font)
        boxes.append(draw.textbbox(origin, text, font=font))
    output = io.BytesIO(); image.save(output, 'JPEG', quality=90)
    return {'content': [{'type': 'text', 'text': 'Fixture'},
            {'type': 'image', 'mimeType': 'image/jpeg',
             'data': base64.b64encode(output.getvalue()).decode()}]}, boxes


def pixels(result):
    return Image.open(io.BytesIO(base64.b64decode(result['content'][1]['data']))).convert('RGB')


class FilterChecks(unittest.TestCase):
    def test_registered_secrets_read_once_without_transport_configuration(self):
        from evo_omp.integrations import credential_input as ci
        data = {'accounts': {
            'ssh': {'password_file': '/private/shared', 'sudo_password_file': '/private/shared'},
            'rdp': {'password_file': '/private/shared'},
            'browser': {'password_file': '/private/browser', 'totp_file': '/private/otp'}},
            'instances': {'pc': {'codex': {'token_file': '/private/token'}}}}
        secrets = {'/private/shared': 'SSH_RDP_PASSWORD\n', '/private/browser': 'BROWSER_PASSWORD\n',
                   '/private/otp': 'JBSWY3DPEHPK3PXP', '/private/token': 'CODEX_PRIVATE_TOKEN\n'}
        with patch.object(ci, 'account_document', return_value=data), \
                patch.object(ci, 'private_text', side_effect=secrets.__getitem__) as read, \
                patch.object(ci, 'totp', side_effect=['123456', '234567', '345678']), \
                patch.object(context, 'secret_values', ['BOT_PRIVATE_TOKEN']):
            values = privacy.known_values()
        self.assertEqual([c.args[0] for c in read.call_args_list], sorted(secrets))
        for value in [*secrets.values(), '123456', '234567', '345678', 'BOT_PRIVATE_TOKEN']:
            self.assertIn(privacy.fold(value), values)

    def test_real_ocr_masks_labels_tokens_and_known_values_preserving_email(self):
        result, boxes = fixture()
        original = json.dumps(result)
        with patch.object(privacy, 'known_values', return_value=[privacy.fold('PrivateValue78642')]):
            filtered = privacy.filter_result(result)
        image = pixels(filtered)
        for i in (1, 2, 3, 4):
            crop = image.crop(boxes[i])
            self.assertGreater(sum(max(p)<16 for p in crop.getdata()) / (crop.width*crop.height), .995, i)
        for i in (0, 5):
            crop = image.crop(boxes[i])
            self.assertGreater(sum(min(p)>235 for p in crop.getdata()) / (crop.width*crop.height), .5, i)
        self.assertEqual(json.dumps(result), original)

    def test_no_detection_keeps_original_encoded_image(self):
        result, _ = fixture()
        with patch.object(privacy, 'known_values', return_value=[]), patch.object(privacy, 'ocr', return_value=[]):
            self.assertEqual(privacy.filter_result(result), result)

    def test_failure_never_returns_raw_frame_or_error_details(self):
        result, _ = fixture()
        for failure in (RuntimeError('synthetic-secret-value'), subprocess.TimeoutExpired(['secret'],5)):
            with patch.object(privacy, 'known_values', return_value=[]), \
                 patch.object(privacy, 'ocr', side_effect=failure):
                with self.assertRaisesRegex(ValueError, '^Screen privacy filter failed; no screenshot returned$'):
                    privacy.filter_result(result)

    def test_private_config_failure_stops_image_too(self):
        result, _ = fixture()
        with patch.object(privacy, 'known_values', side_effect=ValueError('private-path')):
            with self.assertRaisesRegex(ValueError, 'no screenshot returned'):
                privacy.filter_result(result)

    def test_text_results_untouched_and_global_text_redaction_stays_disabled(self):
        result = {'text': 'password: synthetic-only'}
        with patch.object(privacy, 'ocr', side_effect=AssertionError('Not an image')):
            self.assertIs(privacy.filter_result(result), result)
        self.assertEqual(context.redact_sensitive_text(result['text']), result['text'])

    def test_ocr_uses_only_pipes_one_thread_and_no_secret_argv(self):
        header = b'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        with patch.object(privacy.subprocess, 'run') as run:
            run.return_value.stdout = header
            self.assertEqual(list(privacy.ocr(Image.new('RGB',(32,32),'white'))), [])
            self.assertEqual(run.call_args.args[0][:3], ['/usr/bin/prlimit','--core=0:0','--'])
            self.assertEqual(run.call_args.args[0][4:6], ['stdin','stdout'])
            self.assertEqual(run.call_args.kwargs['env']['OMP_THREAD_LIMIT'], '1')
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            self.assertTrue(run.call_args.kwargs['input'].startswith(b'\x89PNG'))
            run.return_value.stdout = b'not TSV'
            with self.assertRaises(ValueError):privacy.ocr(Image.new('RGB',(32,32)))


class RoutingChecks(unittest.IsolatedAsyncioTestCase):
    async def test_registry_filters_both_browser_and_rdp_without_model_call(self):
        from evo_omp.tools import Registry
        registry = Registry()
        turn = context.Turn('123')
        token = context.current.set(turn)
        self.addCleanup(context.current.reset, token)
        for name, args in (('browser', {'action': 'screenshot'}),
                           ('remote_control', {'protocol': 'rdp', 'action': 'screenshot'})):
            result, boxes = fixture()
            registry.tools[name] = ({}, lambda args: result)
            with patch.object(privacy, 'known_values', return_value=[]):
                filtered = await registry.call(name, args, turn)
            image = pixels(filtered)
            self.assertTrue(max(image.getpixel((boxes[1][0]+10, boxes[1][1]+5)))<16)
        await registry.close()

    @unittest.skipUnless(BROWSER, 'Use --browser for the real bwrap/Headless Shell path')
    async def test_real_browser_registry_filters_pixels_and_keeps_live_values(self):
        from evo_omp.integrations import browser
        from evo_omp.tools import Registry
        html = b'''<style>body{font:20px monospace}input{display:block;width:300px;height:40px;background:magenta;margin:20px}</style>
            <input id="email" value="fixture@example.test"><input id="password" type="password" value="FieldOnlySecret">
            <div id="plain">password: PlaintextFixture9462</div>'''
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(html)
            def log_message(self,*args):pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        registry = Registry()
        turn = context.Turn('123')
        token = context.current.set(turn)
        try:
            with tempfile.TemporaryDirectory(dir='/run/evo-omp',prefix='screen-fixture-') as temp, \
                    patch.object(browser,'PROFILE',Path(temp)/'profile'):
                try:
                    await registry.call('browser',{'action':'open','url':f'http://127.0.0.1:{server.server_port}/'},turn)
                    result = await registry.call('browser',{'action':'evaluate','script':"Object.fromEntries(['email','password','plain'].map(id=>{let e=document.getElementById(id),r=e.getBoundingClientRect();return [id,{x:r.x,y:r.y,w:r.width,h:r.height,value:e.value}]}))"},turn)
                    boxes=json.loads(result['text'])
                    started=time.monotonic()
                    shot=await registry.call('browser',{'action':'screenshot'},turn)
                    image=pixels(shot)
                    def pixel(name,dx=30):
                        b=boxes[name];return image.getpixel((round(b['x']+dx),round(b['y']+b['h']/2)))
                    self.assertTrue(max(pixel('password'))<16)
                    self.assertTrue(max(pixel('plain'))<16)
                    email=pixel('email',280);self.assertGreater(email[0],200);self.assertGreater(email[2],200)
                    after=await registry.call('browser',{'action':'evaluate','script':"document.getElementById('password').value"},turn)
                    self.assertEqual(json.loads(after['text']),'FieldOnlySecret')
                    print(json.dumps({'real_browser_bwrap_registry':True,'screen_seconds':round(time.monotonic()-started,3),
                                      'file_screenshots':0,'model_calls':0,'discord_posts':0,'rdp_connections':0}))
                finally:
                    await registry.close()
        finally:
            context.current.reset(token)
            server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
