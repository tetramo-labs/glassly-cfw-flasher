from . import Image, Profile, run as run_profile

PROFILE = Profile(
    name='8BitDo Zero 2',
    boot_product=0x9018,
    version=108,
    size=0x11e00,
    dat_size=124472,
    header_offset=0xc81c,
    official='v1.08',
    runtime_product=0x9018,
    images={
        'install': Image('zero2/glassly-zero2-v1.08.dat', 'zero2/glassly-zero2-v1.08.plain.bin',
                         '007ca4b94e765ba20bfeb7aaaf7728c14a741b6bac821d74932084b8536334a8',
                         '9c93ce2bbf3bba0a4e41e7505e65b4c0deeedb082668ad384e8653aad3333528'),
        'restore': Image('zero2/official-zero2-v1.08.dat', 'zero2/official-zero2-v1.08.plain.bin',
                         '43a313564ac1125df1de9c5b47c393fb5ebfc4d9ff70790e519fb72e609da808',
                         'e661156ba9871167ab0ab7e40d05ee573a07581e5c5157c6e195ff3a5c4823b0'),
    },
    baselines={
        'e661156ba9871167ab0ab7e40d05ee573a07581e5c5157c6e195ff3a5c4823b0': 'official v1.08',
        '9c93ce2bbf3bba0a4e41e7505e65b4c0deeedb082668ad384e8653aad3333528': 'Glassly',
        'bcb8f2e9b65402dccd767be32ae56a19660a25dbd245e56387b1fdae92b1ea54': 'Glassly (previous, blinking LED)',
        '8a1725b90710223d073754583de314552c6dce8a468cc09633a156c23a079050': 'Glassly name probe',
        '2c86e745cfadb4df8692c58bb1747bd9e957ecfc999f6501cd6450cd2b83fe22': 'Glassly BLE probe',
        '940d562d4552a35d70fa25538ef452708cd1fbed6e930eeb43d8eae720373aa5': 'Glassly notification probe',
    },
)


def run(mode, output, reporter):
    return run_profile(PROFILE, mode, output, reporter)
