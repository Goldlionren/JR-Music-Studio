"""Independently reproduce worker formatting and archive the raw response."""
from .store import canonical,sha,require,StoreError
from .creative_format import check


def record(producer,pid,cid,result,receipt,actor):
    command=producer.get(pid,cid,actor)
    require(command['state'] in ('working','rendering','completed'),'COMMAND_STATE_CONFLICT')
    require(isinstance(receipt,dict) and set(receipt)=={'raw','report'},'INVALID_FORMAT_RECEIPT')
    report=receipt['report'];require(isinstance(report,dict),'INVALID_FORMAT_RECEIPT')
    try:
        expected,verified=check(receipt['raw'],command,lyric_contract='lyric_map' in result,finish_reason=report.get('finish_reason'))
    except (ValueError,TypeError,KeyError) as exc:raise StoreError('INVALID_FORMAT_RECEIPT') from exc
    require(expected==result and verified==report,'FORMAT_RECEIPT_MISMATCH')
    def action(db):
        raw_hash=producer.store._blob(db,receipt['raw'].encode())
        report_hash=producer.store._blob(db,canonical(verified))
        producer.store._event(db,pid,'creative_format_checked',actor,dict(command_id=cid,
            raw_sha256=raw_hash,report_sha256=report_hash,status=verified['status'],operation_count=len(verified['operations']),
            result_sha256=verified['result_sha256'],musical_content_changed=False))
        return report_hash
    return producer.store._operation(actor,cid+'_format_check',dict(op='creative_format_check',project_id=pid,command_id=cid,receipt=receipt),action)
