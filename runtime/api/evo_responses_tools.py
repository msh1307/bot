"""Repair nested Code Mode helper names against the current Responses catalog."""
import json
import re


HELPERS = frozenset(('apply_patch', 'exec_command', 'shell_command', 'write_stdin',
                     'view_image', 'create_goal', 'get_goal', 'update_goal'))


class ResponsesToolRepair:
    def __init__(self, request):
        self.tools = []
        self.declared = set()
        self.reported = set()
        for spec in request.get('tools') or []:
            namespace = spec.get('name') if spec.get('type') == 'namespace' else None
            for tool in spec.get('tools', []) if namespace else [spec]:
                name = tool.get('name')
                if not isinstance(name, str) or tool.get('type') not in ('function', 'custom'):
                    continue
                identity = dict(tool, namespace=namespace)
                self.tools.append(identity)
                self.declared.add((namespace, name))
                if namespace:
                    self.declared.update(((None, namespace + '.' + name),
                                          (None, namespace + '__' + name)))
        self.choice = request.get('tool_choice', 'auto')
        self.executor = next((t for t in self.tools if self.code_mode(t) and self.allowed(t)), None)
        self.helpers = set(HELPERS)
        if self.executor:
            # Only explicit nested tool declarations extend the known helpers.
            # Examples mentioning tools.foo do not authorize an invented tool.
            self.helpers.update(re.findall(
                r'\bdeclare\s+const\s+tools\s*:\s*\{\s*([A-Za-z_]\w*)\s*\(',
                self.executor.get('description', '')))

    @staticmethod
    def code_mode(tool):
        if tool['name'] != 'exec' or tool['namespace'] not in (None, 'functions'):
            return False
        description = tool.get('description', '').lower()
        if 'javascript' not in description or 'tools.' not in description:
            return False
        if tool['type'] == 'custom':
            return True
        schema = (tool.get('parameters') or {}).get('properties') or {}
        return (schema.get('input') or {}).get('type') == 'string'

    def allowed(self, tool):
        choice = self.choice
        if choice == 'none':
            return False
        if not isinstance(choice, dict):
            return True
        names = {tool['name']}
        if tool['namespace']:
            names.update((tool['namespace'] + '.' + tool['name'],
                          tool['namespace'] + '__' + tool['name']))
        if choice.get('type') == 'allowed_tools':
            return any(t.get('name') in names and t.get('namespace') in (None, tool['namespace'])
                       for t in choice.get('tools') or [])
        return choice.get('name') in names and choice.get('namespace') in (None, tool['namespace'])

    def plan(self, item):
        if item.get('type') not in ('function_call', 'custom_tool_call'):
            return None
        name, namespace = item.get('name'), item.get('namespace')
        if not isinstance(name, str) or (namespace, name) in self.declared:
            return None
        if namespace not in (None, 'tools'):
            return None
        helper = name
        if helper.startswith('tools__'):
            helper = helper[len('tools__'):]
        elif helper.startswith('tools.'):
            helper = helper[len('tools.'):]
        elif namespace != 'tools':
            # Bare helpers are already handled by OpenCodex. Repair only this missing alias.
            return None
        direct = [t for t in self.tools if t['name'] == helper
                  and t['namespace'] in (None, 'functions') and self.allowed(t)]
        if len(direct) == 1:
            return direct[0], None
        return ((self.executor, helper)
                if self.executor and helper in self.helpers and not direct else None)

    @staticmethod
    def input_text(arguments, helper):
        try:
            value = json.loads(arguments)
        except (ValueError, TypeError):
            return arguments
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if isinstance(value.get('input'), str):
                return value['input']
            if helper == 'apply_patch' and isinstance(value.get('patch'), str):
                return value['patch']
        raise ValueError('Invalid input for repaired custom tool ' + helper)

    @classmethod
    def compile(cls, arguments, helper):
        if helper == 'apply_patch':
            payload = cls.input_text(arguments, helper)
        else:
            payload = json.loads(arguments)
            if helper == 'shell_command' and isinstance(payload, dict):
                payload = dict(payload)
                if 'cmd' not in payload and isinstance(payload.get('command'), str):
                    payload['cmd'] = payload.pop('command')
        target = 'exec_command' if helper == 'shell_command' else helper
        code = 'const result = await tools.' + target + '(' + json.dumps(payload, ensure_ascii=False) + ');\n'
        if helper == 'view_image':
            return code + 'if (result && result.image_url) { image(result.image_url); } else { text(result); }'
        return code + 'text(result);'

    def item(self, item, added=False):
        plan = self.plan(item)
        if not plan:
            return item
        target, helper = plan
        result = dict(item, name=target['name'])
        result.pop('namespace', None)
        if target['namespace']:
            result['namespace'] = target['namespace']
        if helper or target['type'] == 'custom':
            source = item.get('arguments', '') if item['type'] == 'function_call' else item.get('input', '')
            text = '' if added else (self.compile(source, helper) if helper
                                    else self.input_text(source, target['name']))
            if target['type'] == 'custom':
                result['type'] = 'custom_tool_call'
                result.pop('arguments', None)
                result['input'] = text
            else:
                result['type'] = 'function_call'
                result.pop('input', None)
                result['arguments'] = '' if added else json.dumps({'input': text}, ensure_ascii=False)
        elif item['type'] == 'custom_tool_call':
            # A declared function must retain its JSON argument envelope.
            result['type'] = 'function_call'
            result['arguments'] = '' if added else item.get('input', '')
            result.pop('input', None)
        key = item.get('call_id') or item.get('id')
        if key not in self.reported:
            self.reported.add(key)
            print('responses_tool_repair', json.dumps({'source': item['name'], 'target': target['name'],
                                                       'helper': helper}, ensure_ascii=False), flush=True)
        return result
