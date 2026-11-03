"""Narrow unused-self proof for original two-Key product driver sources."""
import ast

def explicit_key_product(fc, keys):
    """No execution: only two distinct declared scalar names multiplied once."""
    driver=fc.driver
    if driver.type!='SCRIPTED' or not driver.use_self or fc.id_data not in keys or fc.array_index!=0:
        return False
    variables=list(driver.variables)
    if len(variables)!=2:return False
    names=[v.name for v in variables]
    if len(set(names))!=2 or 'self' in names:return False
    try:node=ast.parse(driver.expression,mode='eval').body
    except (SyntaxError,ValueError,TypeError):return False
    if not (isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult)
            and isinstance(node.left,ast.Name) and isinstance(node.right,ast.Name)
            and {node.left.id,node.right.id}==set(names)):
        return False
    targets=[]
    for variable in variables:
        if variable.type!='SINGLE_PROP' or len(variable.targets)!=1:return False
        target=variable.targets[0]
        if target.id not in keys or getattr(target,'use_fallback_value',True) or getattr(target,'is_fallback_used',True):
            return False
        if target.data_path not in {block.path_from_id('value') for block in target.id.key_blocks}:
            return False
        if target.id==fc.id_data and target.data_path==fc.data_path:return False
        targets.append((keys.index(target.id),target.data_path))
    return len(set(targets))==2
