import subprocess, os.path as osp
cmds = [
"CGT_FLAGS=backend=python python %s/../../examples/mnist_newmu.py --epochs=1"%osp.dirname(__file__),
"CGT_FLAGS=backend=native python %s/../../examples/mnist_newmu.py --epochs=1"%osp.dirname(__file__)
]


def run_example(cmd):
    subprocess.check_call(cmd, shell=True, stdout=subprocess.PIPE)

def test_examples():
    for cmd in cmds:
        yield run_example,cmd
