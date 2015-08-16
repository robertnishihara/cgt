from cgt.core import *
from . import impls
from collections import defaultdict
from .interpreter import SequentialInterpreter

def function(inputs, outputs, dbg=None, updates=None, givens=None):
    assert isinstance(inputs, list), "Inputs must be a list"
    assert all(isinstance(el, Node) for el in inputs), "Invalid input: should be a list of nodes"

    if isinstance(outputs, list): 
        assert all(isinstance(el, Node) for el in outputs), "Invalid output: should all be symbolic variables"
        return _function_listout(inputs, outputs, dbg, updates, givens)
    elif isinstance(outputs, Node):         
        f_listout = _function_listout(inputs, [outputs], dbg, updates, givens)
        return lambda *args : f_listout(*args)[0]
    else:
        raise ValueError("Expected `outputs` to be a Node or a list of Nodes. Got an object of type %s"%type(outputs))

def _function_listout(inputs, outputs, dbg = None, updates=None, givens=None):
    if updates is None:  updates = []
    else: assert (isinstance(updates, list) and 
                all(isinstance(a,tuple) and len(a)==2 
                    and isinstance(a[0],Node) and isinstance(a[1],Node) 
                    for a in updates)), "updates should be a list of pairs (before, after)"
    if givens is None: givens = []
    else: assert all(isinstance(before, Data) for (before,_) in updates), "lhs of updates must be Data instances"

    if dbg: raise Todo("debug functionality is broken")
    
    outputs = [cgt.make_tuple(*x) if isinstance(x, tuple) else x for x in outputs]
    
    interp = run_compilation_pipeline(inputs, outputs, updates, givens)
    return interp

# ================================================================
# Execution
# ================================================================

def determine_devices(nodes_sorted, node2memowner):
    # Op definitions (available impls, inplace-ness, etc) define constraints
    # on possible devices for a node

    # (1) Get available devices for nodes, determined by which impls are available and node types
    compile_info = impls.get_compile_info()

    cuda_enabled = load_config()["enable_cuda"]
    if cuda_enabled:
        assert compile_info["CGT_ENABLE_CUDA"], "CUDA requested in configuration, but CGT is not compiled with CUDA support"

    node2availabledevs = {}
    node2availableimpltypes = {}
    for node in nodes_sorted:
        pass
        # node2availabledevs
        # devs = None
        # if node2forceddev is not None and node in node2forceddev:
        #     devs = [node2forceddev[node]]
        # elif node.is_argument():
        #     devs = [Device(devtype="cpu")]
        # elif node.is_data():
        #     devs = [node.get_device()]
        # else:
        #     XXX
        #     available_impls = get_available_impls(node).keys()
        #     node2availableimpltypes[node] = available_impls
        #     devs = []
        #     if "impl_py" in available_impls or "impl_c" in available_impls:
        #         devs.append(Device(devtype="cpu"))
        #     if "impl_cuda" in available_impls:
        #         devs.append(Device(devtype="gpu"))
        #     if not devs:
        #         raise RuntimeError("Node %s has no implementations" % node)
        # assert devs
        # node2availabledevs[node] = devs

    # Constraints on devices for output locations (given by node2memowner) must be satisfied
    # so prune devices here
    node2candidatedevs = {}
    for node in node2availabledevs:
        node2candidatedevs[node] = set(node2availabledevs[node])
    for node in nodes_sorted:
        node2candidatedevs[node] &= node2candidatedevs[node2memowner[node]]
        if not node2candidatedevs[node]:
            raise RuntimeError("No available device for node %s, because of memory owner %s" % (node, node2memowner[node]))

    # Constraints on inputs can be satisfied by inserting Transports

    # Greedy assignment: assign as many nodes to GPU as possible
    node2dev = {}
    node2impltype = {}
    for node in nodes_sorted:
        # As nodes get assigned, we need to propagate constraints
        if node2memowner[node] in node2dev:
            node2candidatedevs[node] = [node2dev[node2memowner[node]]]
        assert node2candidatedevs[node] # every node should have at least one device
        best_dev = None
        best_impl = None
        for d in node2candidatedevs[node]:
            if d.devtype == "gpu":
                best_dev = d
                best_impl = "impl_cuda"
            elif d.devtype == "cpu" and best_dev is None:
                best_dev = d
                best_impl = None
                if node in node2availableimpltypes:
                    best_impl = "impl_c" if "impl_c" in node2availableimpltypes[node] else "impl_py"
        node2dev[node] = best_dev
        node2impltype[node] = best_impl

    # In the future, the assignment can take constraints into account to minimze the number of needed transports

    return node2dev, node2impltype

def is_tensor(x):
    return isinstance(x.typ, TensorType)
def is_tuple(x):
    return isinstance(x.typ, TupleType)

def create_interpreter(inputs, outputs, eg, node2memloc):
    assert isinstance(eg, ExecutionGraph)
    input_types = [input.get_type() for input in inputs] #pylint: disable=W0622
    output_locs = [node2memloc[node] for node in outputs]

    backend = load_config()["backend"]
    parallel_interp = load_config()["parallel_interp"]
    if backend == "python":
        if parallel_interp:
            raise NotImplementedError
            # return ParallelInterpreter(eg, output_locs, input_types)
        else:
            return SequentialInterpreter(eg, output_locs, input_types)
    elif backend == "native":
        import cycgt2 #pylint: disable=F0401
        if parallel_interp:
            return cycgt2.CppInterpreterWrapper(eg, input_types, output_locs, True)
        else:
            return cycgt2.CppInterpreterWrapper(eg, input_types, output_locs, False)
    else:
        raise NotImplementedError("invalid backend %s"%backend)

def topsorted_shapes_first(outputs, node2shape):
    # Almost identical to topsorted(...) function
    # But we also need to visit the shape elements of an in-place node
    # before visiting that node
    marks = {}
    out = []
    stack = [] 
    for x in outputs:
        stack.append((x,0))
        while stack:
            (i,jidx) = stack.pop()
            if jidx == 0:
                m = marks.get(i,0)
                if m == 0:
                    marks[i] = 1
                elif m == 1:
                    raise ValueError("not a dag")
                else:
                    continue
            ps = i.parents
            ###### Changed part ######
            if i.ndim > 0 and not i.is_input() and i.op.call_type=="byref":
                if i in node2shape:
                    shpels = node2shape[i]
                else:
                    raise Unreachable
                    # shpels = i.op.shp_apply(i.parents)
                ps = ps + shpels
            elif is_tuple(i):
                for arrshp in node2shape[i]:
                    ps = ps + arrshp
            ##########################
            if jidx == len(ps):
                marks[i] = 2
                out.append(i)
            else:
                stack.append((i,jidx+1))
                j = ps[jidx]
                stack.append((j,0))
    return out

def determine_memowner(nodes_sorted, updates):
    # First determine how many "child" nodes each node has
    node2child = defaultdict(list)
    for node in nodes_sorted:
        for parent in node.parents:
            node2child[parent].append(node)

    # Now traverse graph again and see where we can use the same memory
    node2memowner = {} # mapping node x -> the node that owns its memory
    
    # For updates, memlocation(RHS) = memlocation(LHS)
    after2before = {after:before for (before,after) in updates}

    enable_inplace_opt = load_config()["enable_inplace_opt"]

    for node in nodes_sorted:

        base = node # by default, 
        if node.is_argument():
            pass
        elif node.op.writes_to_input >= 0:
            base = node2memowner[node.parents[node.op.writes_to_input]]
        elif node in after2before:
            base = after2before[node]
        elif enable_inplace_opt and node.op.call_type == "byref": # TODO think about if we need any other conditions
            nodeshape = node.op.shp_apply(node.parents)
            for parent in node.parents:
                if (len(node2child[parent])==1
                        and nodeshape==cgt.shape(parent) # XXX not a very robust way to check
                        and node.dtype == parent.dtype
                        and _is_data_mutable(parent)):
                    base = parent
                    break
        # TODO: add optimization for in-place incrementing
        node2memowner[node] = base

    return node2memowner

class MemCounter(object):
    """
    returns `MemLocation`s with indices 0,1,...
    `count` member indicates how many have been returned thus far
    """
    def __init__(self):
        self.count=0
    def new_memloc(self, devtype):
        out = MemLocation(self.count, devtype)
        self.count += 1
        return out


def create_execution_graph(inputs, nodes_sorted, node2shape, node2memowner, node2dev):
    # node2impltype = copy.copy(node2impltype) # we'll insert transport ops
    instrs = []
    counter = MemCounter()
    node2memloc = {}

    for node in nodes_sorted:
        if node.is_argument():
            write_loc = counter.new_memloc(node2dev[node].devtype)
            node2memloc[node] = write_loc
            i = inputs.index(node)
            instrs.append(LoadArgument(i, write_loc))
        else:
            # Transports for reads
            read_locs = [node2memloc[parent] for parent in node.parents]
            # for parent in node.parents:
            #     read_loc = node2memloc[parent]
                # if read_loc.devtype != node2dev[node].devtype:
                #     # Allocation instr for the transport destination
                #     transported_loc = counter.new_memloc(node2dev[node].devtype)
                #     parent_shape = node2shape[parent] if parent.ndim > 0 else []
                #     parent_shape_locs = [node2memloc[shpel] for shpel in parent_shape]
                #     instrs.append(Alloc(node.dtype, parent_shape_locs, transported_loc))
                #     # The transport instruction
                #     tmp_transport_node = Result(TransportToOutputDevice(), [parent])
                #     assert read_loc.devtype != transported_loc.devtype
                #     instrs.append(ReturnByRef(tmp_transport_node, [read_loc], transported_loc))
                #     read_locs.append(transported_loc)
                #     node2impltype[tmp_transport_node] = "impl_c"
                #     # node2dev[tmp_transport_node] = Device(devtype="cpu")
                # else:
                #     read_locs.append(read_loc)
            assert len(read_locs) == len(node.parents)

            if node.op.call_type == "byref":
                if node2memowner[node] == node:
                    if is_tensor(node): # just make one memory location for output
                        nodeshape = node2shape[node] if node.ndim > 0 else []
                        shape_locs = [node2memloc[shpel] for shpel in nodeshape]
                        write_loc = counter.new_memloc(node2dev[node].devtype)                    
                        instrs.append(Alloc(node.dtype, shape_locs, write_loc))
                    else: # if it's a tuple, we need to allocate all of the components, then build tuple
                        nodeshape = node2shape[node]
                        assert isinstance(nodeshape, tuple)
                        arr_locs = []
                        for (arrshp, arrtyp) in utils.safezip(nodeshape, node.get_type()):
                            arr_loc = counter.new_memloc(node2dev[node].devtype)
                            shape_locs = [node2memloc[shpel] for shpel in arrshp]
                            instrs.append(Alloc(arrtyp.dtype, shape_locs, arr_loc))
                            arr_locs.append(arr_loc)
                        write_loc = counter.new_memloc(node2dev[node].devtype)
                        instrs.append(BuildTup(node.get_type(), arr_locs, write_loc))

                else:
                    # If this node writes to another node's memory, the devices must be the same
                    # this should have been enforced in determine_devices()
                    assert node2dev[node] == node2dev[node2memowner[node]]
                    write_loc = node2memloc[node2memowner[node]]                
                instrs.append(ReturnByRef(node.op, [par.typ for par in node.parents], read_locs, write_loc))
            else:
                assert node.op.call_type == "byval"
                write_loc = counter.new_memloc(node2dev[node].devtype)
                instrs.append(ReturnByVal(node.op, [par.typ for par in node.parents], read_locs, write_loc))
        node2memloc[node] = write_loc
    return ExecutionGraph(instrs, len(inputs), counter.count), node2memloc


def get_callable(op, input_types, devtype):
    assert op.available_impls, "need to set op.available_impls"
    if load_config()["backend"] == "python" and "python" in op.available_impls or devtype=="cpu" and "native_cpu" not in op.available_impls:
        return op.get_py_callable(input_types)
    else:
        nci = op.get_native_compile_info(input_types, devtype)
        return impls.nci2callable(nci)

def run_compilation_pipeline(inputs, outputs, updates, givens):
    """
    Compiles the expression graph into an execution graph. 
    """
    # Phase 1: simplification and analysis of expression graph
    # ------------------------------------------------------
    # Add add update targets to outputs
    outputs_updatetargs = outputs + [after for (_before, after) in updates]
    if givens: outputs_updatetargs = clone(outputs_updatetargs, dict(givens))
    # Do simplification + analysis pass on expression graph
    outputs_updatetargs_simple, analysis = \
        simplify_and_analyze(outputs_updatetargs) if load_config()["enable_simplification"] \
        else (outputs_updatetargs, analyze(outputs_updatetargs))

    # Phase 2: build execution graph
    # ------------------------------------------------------
    # Sort nodes so that shape elements appear before a given node
    nodes_sorted = topsorted_shapes_first(outputs_updatetargs_simple, analysis["node2shape"])
    # For each node, figure out if its output should be written to a previous node's memory
    # (memowner : "memory owner")
    updatetargs_simple = outputs_updatetargs_simple[len(outputs):]
    updatesrcs = [before for (before, _) in updates]
    node2memowner = determine_memowner(nodes_sorted, zip(updatesrcs, updatetargs_simple))
    # Determine location and device of nodes
    # node2dev, node2impltype = determine_devices(nodes_sorted, node2memowner)
    node2dev = {node:Device() for node in nodes_sorted}
    # Find the outputs we want to return
    outputs_simple = outputs_updatetargs_simple[:len(outputs)] # get rid
    # Generate execution graph
    eg, node2memloc = create_execution_graph(
        inputs, nodes_sorted, analysis["node2shape"], node2memowner, node2dev)

    # Print execution graph
    # TODO: reintroduce with better logging system
    # print 'begin'
    # print '\n'.join('\t'+repr(instr) for instr in eg.instrs)
    # print 'end'

    # Phase 3: create C or Python interpreter for graph
    # ------------------------------------------------------
    interp = create_interpreter(inputs, outputs_simple, eg, node2memloc)

    # Done!
    return interp



################################################################
### Simple numeric eval via traversal 
################################################################
 
def numeric_eval(output, arg2val):
    if isinstance(output, list):
        assert all(isinstance(x, Node) for x in output), "expected a list of Nodes"
        return _numeric_eval_listout(output, arg2val)
    elif isinstance(output, Node):
        return _numeric_eval_listout([output],arg2val)[0]
    else:
        raise ValueError("expected `output` to be a Node or a list of Nodes. Got an object of type %s"%type(output))

def _numeric_eval_listout(outputs, arg2val):
    """
    Evaluate outputs numerically. arg2val is a dictionary mapping arguments to numerical values
    """
    assert isinstance(outputs, list)
    assert isinstance(arg2val, dict)

    nodes = list(topsorted(outputs))

    node2val = {}
    for node in nodes:
        if node.is_argument():
            node2val[node] = arg2val[node]
        elif node.is_data():
            node2val[node] = node.get_value()
        else:
            parentvals = [node2val[par] for par in node.parents]
            node2val[node] = py_numeric_apply(node, parentvals)
        # assert node.get_ndim() == np.array(node2val[node]).ndim
    numeric_outputs = [node2val[node] for node in outputs]

    return numeric_outputs

################################################################
### Execution graph 
################################################################

MemInfo = namedtuple("MemInfo",["loc","access"])
MEM_OVERWRITE = "overwrite"
MEM_INCREMENT = "increment"

class ExecutionGraph(object):
    def __init__(self, instrs, n_args, n_locs):
        self.instrs = instrs
        self.n_args = n_args
        self.n_locs = n_locs

class MemLocation(object):
    def __init__(self, idx, devtype):
        assert isinstance(idx, int) and devtype in ["cpu", "gpu"]
        self.index = idx
        self.devtype = devtype
        # TODO: dtype
    def __repr__(self):
        return "%%%i/%s" % (self.index, self.devtype)



################################################################
### Instructions 
################################################################

class Instr(object):
    def fire(self, interp):
        raise NotImplementedError

class LoadArgument(Instr):
    def __init__(self, ind, write_loc):
        self.ind = ind
        self.write_loc = write_loc
    def fire(self, interp):
        interp.set(self.write_loc, interp.getarg(self.ind))
    def __repr__(self):
        return "%s = LoadArg ind:%i" % (self.write_loc, self.ind)

class Alloc(Instr):
    def __init__(self, dtype, read_locs, write_loc):
        self.dtype = dtype
        self.read_locs = read_locs
        self.write_loc = write_loc
    def fire(self, interp):
        shp = tuple(interp.get(mem) for mem in self.read_locs)
        prevarr = interp.get(self.write_loc)
        if prevarr is None or prevarr.shape != shp: 
            interp.set(self.write_loc, np.ones(shp, self.dtype))
    def __repr__(self):
        return "%s = Alloc shp:%s dtype:%s" % (self.write_loc, str(self.read_locs), self.dtype)

class BuildTup(Instr):
    def __init__(self, typ, read_locs, write_loc):
        self.typ = typ
        self.read_locs = read_locs
        self.write_loc = write_loc
    def fire(self, interp):
        interp.set(self.write_loc, tuple(interp.get(loc) for loc in self.read_locs))
    def __repr__(self):
        return "%s = BuildTup:%s" % (self.write_loc, self.typ)

class ReturnByRef(Instr):
    def __init__(self, op, input_types, read_locs, write_loc):
        self.op = op
        self.input_types = input_types
        self.read_locs = read_locs
        self.write_loc = write_loc
        self._callable = None
    def fire(self, interp):
        self.get_callable().call(
            [interp.get(mem) for mem in self.read_locs],
            interp.get(self.write_loc))
    def __repr__(self):
        return "%s = ReturnByRef op:%s args:%s" % (self.write_loc, str(self.op), str(self.read_locs))
    def get_callable(self):
        if self._callable is None: self._callable = get_callable(self.op, self.input_types, "cpu")
        return self._callable

class ReturnByVal(Instr):
    def __init__(self, op, input_types, read_locs, write_loc):
        self.op = op
        self.input_types = input_types
        self.read_locs = read_locs
        self.write_loc = write_loc
        self._callable = None
    def fire(self, interp):
        interp.set(self.write_loc, self.get_callable().call([interp.get(mem) for mem in self.read_locs]))
    def get_callable(self):
        if self._callable is None: self._callable = get_callable(self.op, self.input_types, "cpu")
        return self._callable
    def __repr__(self):
        return "%s = ReturnByVal op:%s args:%s" % (self.write_loc, str(self.op), str(self.read_locs))

################################################################
### Utils 
################################################################
 
def _list_to_json(xs):
    return [x.to_json() for x in xs]

def _is_data_mutable(node):
    if isinstance(node, Result):
        return not isinstance(node.op, Constant) 
    elif isinstance(node, Input):
        return False
    else:
        raise RuntimeError


