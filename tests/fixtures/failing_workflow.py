from godel import workflow, WorkflowFail

@workflow
async def my_workflow():
    raise WorkflowFail("intentional failure")
