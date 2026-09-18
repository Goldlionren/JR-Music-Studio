"""Package worker code only; credentials and SSH configuration remain private."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sources = [ROOT/'integrations/hermes'/name for name in
               ('producer_worker.py', 'mcp_broker.py', 'mcp_target.py')]
    sources += [ROOT/'jr_music'/name for name in ('client.py', 'creative_format.py')]
    for source in sources:
        target = args.output/source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError('Refusing to overwrite changed file: ' + str(target))
    for source in sources:
        shutil.copyfile(source, args.output/source.name)
    print('Worker files prepared; add private client-config.json on the Hermes host.')


if __name__ == '__main__':
    main()
