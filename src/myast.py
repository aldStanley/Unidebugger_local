import javalang
from apted import APTED, Config

def code2ast(code):
    return javalang.parse.parse(code)

def ast_to_tuple(node):
    if isinstance(node, javalang.ast.Node):
        children = [ast_to_tuple(child) for _, child in node.children() if isinstance(child, javalang.ast.Node)]
        return (node.__class__.__name__, children)
    return None

def ast_dis(tree1: javalang.ast.Node, tree2: javalang.ast.Node):
    tuple1, tuple2 = ast_to_tuple(tree1), ast_to_tuple(tree2)
    apted = APTED(tuple1, tuple2)
    return apted.compute()

def _type_str(type_node) -> str:
    if type_node is None:
        return "void"
    try:
        name = type_node.name
        args = getattr(type_node, 'arguments', None)
        if args:
            arg_strs = [_type_str(a.type) if hasattr(a, 'type') and a.type is not None else "?" for a in args]
            name = f"{name}<{', '.join(arg_strs)}>"
        dims = getattr(type_node, 'dimensions', None)
        if dims:
            name += "[]" * len([d for d in dims if d is not None])
        return name
    except Exception:
        return str(type_node)

def extract_method_stubs(code: str) -> str:
    """
    Parse Java source and return only class/method signatures without bodies.
    Falls back to the original code string if parsing fails.
    """
    try:
        tree = javalang.parse.parse(code)
    except Exception:
        return code

    lines = []

    def process_type(type_decl):
        kind = "interface" if isinstance(type_decl, javalang.tree.InterfaceDeclaration) else "class"
        lines.append(f"{kind} {type_decl.name} {{")
        for ctor in getattr(type_decl, 'constructors', []):
            params = ", ".join(f"{_type_str(p.type)} {p.name}" for p in ctor.parameters)
            lines.append(f"    {type_decl.name}({params});")
        for method in getattr(type_decl, 'methods', []):
            ret = _type_str(method.return_type)
            params = ", ".join(f"{_type_str(p.type)} {p.name}" for p in method.parameters)
            lines.append(f"    {ret} {method.name}({params});")
        lines.append("}")

    for _, node in tree.filter(javalang.tree.ClassDeclaration):
        process_type(node)
    for _, node in tree.filter(javalang.tree.InterfaceDeclaration):
        process_type(node)

    return "\n".join(lines) if lines else code

    
