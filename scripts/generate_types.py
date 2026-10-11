"""Generate Go constants and public TypeScript SDK types from one local schema."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import sys
import tempfile
from common import ROOT, pnpm_command

def public_schema(schema):
    """Hidden fields are available to Go but never returned by the public API."""
    return [dict(collection, fields=[field for field in collection['fields']
            if not field.get('hidden') and field['type'] != 'password'])
            for collection in schema]


def select_option_key(value):
    """Keep pocketbase-typegen's numeric option key convention (E1, E2, ...)."""
    stripped = value.strip()
    numeric = not stripped or bool(re.fullmatch(
        r'[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)'
        r'|0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+', stripped,
    ))
    return f'E{value}' if numeric else value


def prepare_frontend_schema(schema):
    """Substitute safe option tokens before the upstream generator sees values.

    pocketbase-typegen 1.5 interpolates select values into quoted TypeScript
    strings without escaping. Replacing its safe token lines afterward lets us
    preserve quotes, newlines, and backslashes in both keys and values.
    """
    result = public_schema(schema)
    replacements = {}
    for collection in result:
        fields = []
        for original in collection['fields']:
            field = dict(original)
            if field['type'] == 'select':
                values = []
                seen_values = set()
                seen_keys = {}
                for value in field.get('values', []):
                    if not isinstance(value, str):
                        raise ValueError(f"Select values must be strings: {collection['name']}.{field['name']}")
                    if value in seen_values:
                        continue
                    seen_values.add(value)
                    key = select_option_key(value)
                    if key in seen_keys:
                        raise ValueError(f"TypeScript select key {key!r} collides between {seen_keys[key]!r} and {value!r} "
                                         f"in {collection['name']}.{field['name']}; rename an option")
                    seen_keys[key] = value
                    token = f'__pb_typegen_option_{len(replacements)}__'
                    replacements[token] = (key, value)
                    values.append(token)
                field['values'] = values
            fields.append(field)
        collection['fields'] = fields
    return result, replacements


def normalize_types(source, replacements=None):
    # Only remove the obsolete username in the generated auth helper. An app
    # may intentionally have its own username field in a collection record.
    pattern = r'(export type AuthSystemFields[^\{]*\{\n)(.*?)(\n\} & BaseSystemFields<[^>]*>)'
    source = re.sub(pattern, lambda match: match[1] + match[2].replace('\tusername: string\n', '') + match[3],
                    source, flags=re.DOTALL)
    for token, (key, value) in (replacements or {}).items():
        line = f'\t"{token}": "{token}",'
        if source.count(line) != 1:
            raise ValueError(f'Unexpected pocketbase-typegen select output for {token}; update the compatibility wrapper')
        source = source.replace(line, f'\t{json.dumps(key)}: {json.dumps(value)},')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check committed output without writing')
    options, extra = parser.parse_known_args()
    command = ['go', 'run', '.', 'types', '--output', 'collections/collections.go',
               '--schema-output', 'schema.json', *extra]
    if options.check:
        command.append('--check')
    subprocess.run(command, cwd=ROOT / 'pocketbase', check=True)
    schema = json.loads((ROOT / 'pocketbase/schema.json').read_text())
    frontend_schema, replacements = prepare_frontend_schema(schema)
    with tempfile.TemporaryDirectory(prefix='pb-template-types-') as directory:
        temporary = Path(directory)
        schema_file = temporary / 'schema.json'
        output = temporary / 'types.ts'
        schema_file.write_text(json.dumps(frontend_schema))
        subprocess.run(
            [*pnpm_command(), 'exec', 'pocketbase-typegen', '--json', str(schema_file), '--out', str(output)],
            cwd=ROOT, check=True,
        )
        generated = normalize_types(output.read_text(), replacements)
    destination = ROOT / 'src/lib/pocketbase-types.ts'
    if options.check:
        if not destination.exists() or destination.read_text() != generated:
            raise ValueError('TypeScript types are out of date; run make types')
    else:
        if not destination.exists() or destination.read_text() != generated:
            destination.write_text(generated)


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f'Type generation failed: {error}', file=sys.stderr)
        sys.exit(1)
