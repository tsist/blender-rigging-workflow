# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable schematic face and section captions for a native mouth board."""
import math
import bpy
from . import face_mouth_pad as pad, face_graph_board as graph, face_expression_layout as layout


def install(hub, *, font=None):
    group, board, rig, _ = pad._read(hub)
    display = hub['board_display']
    pad._require(not any(o.get('c94_board_art_v1') for o in display.objects), '示意脸/分区标题已存在，可直接原生编辑其Curve/Text')
    original = rig.pose.bones[board['root']].custom_shape
    objects = []
    try:
        cx,cy=-board['travel']-.075,.035
        verts=[(cx+.026*math.cos(i*math.tau/40),cy+.043*math.sin(i*math.tau/40),0) for i in range(40)]
        edges=[(i,(i+1)%40) for i in range(40)]
        for x,y,w in [(cx-.016,cy+.009,.011),(cx+.005,cy+.009,.011),(cx-.01,cy-.015,.02)]:
            off=len(verts);verts.extend([(x,y,0),(x+w,y,0)]);edges.append((off,off+1))
        art=graph._shape('面部示意_'+group['group_id'],verts,edges,rig,board['root'],display,objects)
        # Keep the root's rectangle widget; the schematic is an editable Curve.
        rig.pose.bones[board['root']].custom_shape=original
        for obj in objects:obj['c94_board_art_v1']=True
        declaration=layout.read(hub);rows={r['channel']:r for r in declaration['rows']}
        sections={}
        inverse=rig.pose.bones[board['root']].matrix.inverted()
        for row in board['sliders']:
            section=rows[row['name']]['section'];p=inverse@rig.pose.bones[row['anchor']].matrix.translation
            sections.setdefault(section,[]).append(p)
        for section,points in sections.items():
            obj=graph._shape('分区_'+section,None,None,rig,board['root'],display,objects,font=font,text=section,color=(.9,.75,.4,1))
            obj.location=(min(p.x for p in points)-.025,max(p.y for p in points)+board['travel']+.025,.002)
            obj.data.size=.012;obj['c94_board_art_v1']=True
        pad.fit_frame(hub)
        return [o.name for o in objects if o.type in {'CURVE','FONT'}]
    except Exception:
        rig.pose.bones[board['root']].custom_shape=original
        graph._destroy(objects)
        raise
