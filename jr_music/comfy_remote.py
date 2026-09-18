"""Remote MCP adapter; JR verifies history/downloads against the frozen target."""
from .comfy_local import LocalComfy
from .store import canonical, require, sha
from .render_template import graph_only


class RemoteComfy(LocalComfy):
    def __init__(self, renders, local, routing, binding):
        super().__init__(renders,local.cli,local.work_root,actor=local.actor,
                         environment_probe=lambda:routing.target(binding,'probe')['environment'])
        self.routing,self.binding=routing,binding
        self.server=dict(server_id=binding['server_id'],url=binding['url'],hardware=binding['hardware'])

    def stage(self, project_id, render_id):
        graph=graph_only(self.build(project_id,render_id))
        packet=self.routing.target(self.binding,'prepare',render_id=render_id,workflow=graph)
        self.renders.bind_environment(project_id,render_id,packet['environment'],actor=self.actor)
        require(packet['workflow_sha256']==sha(canonical(graph)),'PREFLIGHT_WORKFLOW_MISMATCH')
        self.renders.bind_workflow(project_id,render_id,graph,dict(valid=True,server_id=self.server['server_id'],
            workflow_sha256=packet['workflow_sha256'],official_validation=packet['official_validation']),actor=self.actor)
        return packet['workflow_path']
