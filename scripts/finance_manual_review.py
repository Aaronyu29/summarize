"""Human-review documents and explicit, bounded correction of saved candidates.

Drafts never enter the accepted research archive or change review status to complete.
Only a separately reverified correction may enter the ordinary publication path.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import pathlib
import re
import os
from finance_feishu import Publisher
from finance_jobs import Jobs
from finance_alerts import enqueue
from finance_recovery import atomic, advisory_lock, ReviewRejected


def read(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def receipt_candidate(path):
    choice = read(path)['choices'][0]
    if choice.get('finish_reason') != 'stop':
        raise ValueError('Saved candidate is incomplete')
    result = json.loads(choice['message']['content'])
    if not isinstance(result, dict) or not isinstance(result.get('claims'), list):
        raise ValueError('Saved candidate schema invalid')
    return result


def load_correction(capture, vid):
    path = capture / 'user-correction.json'
    correction = read(path)
    name = correction['source_candidate_file']
    if pathlib.Path(name).name != name:
        raise ValueError('Correction candidate path escapes evidence directory')
    source = capture / vid / name
    if digest(source) != correction['source_candidate_sha256']:
        raise ValueError('Original candidate changed')
    frames = {f['timestamp_seconds']: f for f in read(capture / vid / 'frames.json')}
    frame = frames[correction['frame_seconds']]
    image = (capture / vid / frame['path']).resolve()
    if not image.is_relative_to((capture / vid).resolve()) or digest(image) != frame['sha256']:
        raise ValueError('Correction frame integrity mismatch')
    if frame['sha256'] != correction['frame_sha256'] or correction.get('requires_independent_review') is not True:
        raise ValueError('Correction cannot bypass independent review')
    candidate = receipt_candidate(source)
    segments = {f"S{s['id']:06d}" for s in read(capture/vid/'transcript.asr.json')['segments']}
    seen = set()
    for change in correction['changes']:
        index, field = change['claim_index'], change['field']
        if (type(index) is not int or not 0 <= index < len(candidate['claims'])
                or field not in {'headline', 'text', 'uncertainty','evidence_segment_ids','frame_seconds'} or (index, field) in seen):
            raise ValueError('Correction scope invalid')
        second=change.get('frame_seconds',correction['frame_seconds'])
        expected=change.get('frame_sha256',correction['frame_sha256'])
        frame=frames[second];image=(capture/vid/frame['path']).resolve()
        original_second = candidate['claims'][index].get('frame_seconds')
        if field == 'frame_seconds':
            original = frames[original_second]
            original_image = (capture/vid/original['path']).resolve()
            if (not original_image.is_relative_to((capture/vid).resolve())
                    or digest(original_image) != original['sha256']):
                raise ValueError('Original correction frame integrity mismatch')
        if ((field != 'frame_seconds' and original_second!=second) or frame['sha256']!=expected
                or not image.is_relative_to((capture/vid).resolve()) or digest(image)!=expected):
            raise ValueError('Correction claim frame mismatch')
        seen.add((index, field))
        after=change['after']
        valid=(isinstance(after,list) and bool(after) and all(isinstance(v,str) and v in segments for v in after)
               and len(set(after))==len(after)) if field=='evidence_segment_ids' else isinstance(after,str) and bool(after.strip())
        if field == 'frame_seconds':
            valid = type(after) is int and after == second and after in frames
        if candidate['claims'][index][field] != change['before'] or not valid:
            raise ValueError('Correction original text mismatch')
        candidate['claims'][index][field] = change['after']
    if not seen:
        raise ValueError('Empty correction')
    return candidate, digest(path)


def text(value):
    # Preserve existing bold, but source prose cannot inject images or external links.
    s = str(value or '').replace('FINANCE_IMAGE_', '原始图像标识_')
    for old, new in [('<', '&lt;'), ('>', '&gt;'), ('[', '\\['), (']', '\\]'), ('`', '\\`')]:
        s = s.replace(old, new)
    return s


def create_review_document(capture, source, checkpoint):
    vid = source['video_id'];directory = capture / vid
    state = read(capture / 'analysis-review-state.json') if (capture / 'analysis-review-state.json').exists() else {}
    issues = state.get('issues') or checkpoint.get('review_issues') or ['自动流程未完成，请核查原始材料。']
    candidate = None
    if (capture / 'user-correction.json').exists():
        candidate, _ = load_correction(capture, vid)
    elif state.get('candidate_file'):
        name = state['candidate_file']
        if pathlib.Path(name).name != name:raise ValueError('Unsafe saved candidate name')
        candidate = receipt_candidate(directory / name)
    else:
        # Structural validation may fail before a semantic-review state is written.
        # Show the retained candidate as unverified, without inventing source references.
        paths=sorted(directory.glob('analysis*.response.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for path in paths:
            try:candidate=receipt_candidate(path);break
            except (KeyError,TypeError,ValueError):continue
    publication = source.get('published_utc', '')
    if publication:
        publication = dt.datetime.fromisoformat(publication.replace('Z', '+00:00')).astimezone(
            dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    lines = ['# 待人工审核｜' + text(source.get('title', vid)), '',
        '**自动证据审核未通过，以下内容是待核对草稿。**',
        '人工确认前保持待核查状态，不作为已核实的买卖依据。', '',
        '视频发布：' + (publication or '待确认') + '（北京时间）',
        '[原视频](https://www.youtube.com/watch?v=' + vid + ')', '', '## 优先审核这些问题', '']
    for i, issue in enumerate(issues, 1):
        lines += [f'### 争议 {i}', '', '**请核对原图与原话：** ' + text(issue), '']
    if (capture / 'user-correction.json').exists():
        lines += ['**已按原图做局部纠错，仍须核对下列独立审核结果。**', '']
    frames = {f['timestamp_seconds']: f for f in read(directory / 'frames.json')} if (directory / 'frames.json').exists() else {}
    transcript = read(directory / 'transcript.asr.json') if (directory / 'transcript.asr.json').exists() else {}
    segments = {s['id']: s for s in transcript.get('segments', [])}
    if candidate:
        lines += ['## 未核实草稿全文', '', text(candidate.get('overview')), '']
        for i, claim in enumerate(candidate['claims']):
            lines += [f"### 第{i+1}项（原始索引{i}）｜" + text(claim.get('headline')), '',
                      text(claim.get('text')), '', '**待核对说明：** ' + text(claim.get('uncertainty') or '仍须人工核对。'), '']
            second = claim.get('frame_seconds')
            if second in frames:
                frame = frames[second];image = (directory / frame['path']).resolve()
                if not image.is_relative_to(directory.resolve()) or digest(image) != frame['sha256']:
                    raise ValueError('Human review frame integrity mismatch')
                lines += [f'![原始画面 {second} 秒]({vid}/{frame["path"]})', '',
                          f'[回看对应画面](https://www.youtube.com/watch?v={vid}&t={int(second)}s)', '']
            refs = [int(v[1:]) if isinstance(v,str) and re.fullmatch(r'S\d+',v) else v
                    for v in claim.get('evidence_segment_ids', [])]
            excerpts = [segments[v]['text'] for v in refs if type(v) is int and v in segments]
            if excerpts:lines += ['**对应自动转写（可能有识别错误）：**', text('\n'.join(excerpts)), '']
        lines += ['## 后续复盘问题（未核实）', '', text(candidate.get('review_questions')), '']
    else:
        lines += ['## 已有材料', '', '**尚无完整分析稿。** 请先查看原视频；本文件不冒充完整报告。', '']
    content = '\n'.join(lines);identity = hashlib.sha256(content.encode()).hexdigest()[:16]
    path = capture / ('manual-review-' + identity + '.md')
    if path.exists() and path.read_text(encoding='utf-8') != content:raise ValueError('Review draft collision')
    path.write_text(content,encoding='utf-8')
    return path


def handoff(jobs, vid, root, env, publisher=None):
    guard=advisory_lock(pathlib.Path(root)/'videos'/vid/'human-review.lock')
    try:guard.__enter__()
    except BlockingIOError:return {'status':'review_in_progress'}
    try:return _handoff(jobs,vid,root,env,publisher)
    finally:guard.__exit__(None,None,None)


def _handoff(jobs, vid, root, env, publisher):
    job = jobs.get(vid)
    if not job or job['status'] != 'needs_review':return None
    old = job['checkpoint'].get('human_review_delivery') or {}
    if old.get('status') in {'delivered','already_delivered'}:return old
    capture = pathlib.Path(job['checkpoint'].get('capture') or pathlib.Path(root)/'videos'/vid)
    try:
        if not capture.resolve().is_relative_to((pathlib.Path(root)/'videos').resolve()):
            raise ValueError('Review capture escapes video archive')
        capture.mkdir(parents=True, exist_ok=True)
        report = create_review_document(capture,job['source'],job['checkpoint'])
        result = (publisher or Publisher)(report,'【待人工审核】'+job['source'].get('title',vid),'feishu',env=env).run()
        if result.get('status') not in {'delivered','already_delivered'}:raise ValueError('Review document not delivered')
        jobs.save(vid,human_review_delivery=result,human_review_report=str(report))
        return result
    except Exception as error:
        jobs.save(vid,human_review_error=type(error).__name__)
        enqueue(root,job['source'],'人工审核文档发布',error,
                '人工审核文档尚未交付；草稿和原始回执已保留，请核查发布状态。',
                identity_key=['human-review-missing',vid])
        return {'status':'review_document_failed','error':type(error).__name__}


def repair(jobs, vid, root, env):
    with advisory_lock(pathlib.Path(root)/'videos'/vid/'human-review.lock'):
        return _repair(jobs,vid,root,env)


def _repair(jobs, vid, root, env):
    from finance_cloud import (review_candidate, canonicalize_evidence, publication_time,
        validate, validate_readability, REPORT_STYLE_VERSION, finalize_analysis, delivery_title)
    job=jobs.get(vid)
    if job and job['status']=='complete':
        delivery=job['checkpoint'].get('delivery') or {}
        if job['checkpoint'].get('human_correction_verified') and delivery.get('status') in {'delivered','already_delivered'}:
            return {**delivery, 'status':'already_delivered'}
        raise ValueError('Completed job cannot be reset by correction')
    if not job or job['status']!='needs_review':raise ValueError('Correction requires a held review job')
    capture=pathlib.Path(job['checkpoint'].get('capture') or pathlib.Path(root)/'videos'/vid)
    if not capture.resolve().is_relative_to((pathlib.Path(root)/'videos').resolve()):
        raise ValueError('Correction capture escapes video archive')
    candidate,correction_hash=load_correction(capture,vid);directory=capture/vid
    proof=capture/'reviewed-user-correction.json'
    if proof.exists():
        sealed=read(proof)
        if sealed['correction_sha256']!=correction_hash:raise ValueError('Verified correction identity changed')
        analysis=sealed['analysis']
    else:
        if job['checkpoint'].get('user_correction_attempt_sha256')==correction_hash:
            raise ValueError('Correction already attempted; inspect retained receipts without paid replay')
        jobs.save(vid,user_correction_attempt_sha256=correction_hash)
        transcript=read(directory/'transcript.asr.json');manifest=read(directory/'frames.json')
        info=read(directory/(vid+'.info.json'));video={**canonicalize_evidence(candidate,transcript['segments']),
            'video_id':vid,'published_shanghai':publication_time(info,[job['source']])}
        validate_readability(video)
        analysis={'schema_version':1,'revision':1,'edition':'单期研究｜按原图局部纠错后独立复核',
            'report_style_version':REPORT_STYLE_VERSION,'summary':video['overview'],'videos':[video],
            'cross_video_observations':'本报告仅复盘本期视频；历史报价和博主观点不等于当前买卖信号。',
            'review_provenance':{'mode':'explicit_user_correction_then_independent_second_pass',
                'model':env['LLM_MODEL'],'correction_file':'user-correction.json','correction_sha256':correction_hash}}
        validate(analysis,capture)
        payload={'video_id':vid,'title':info['title'],'duration':info['duration'],
            'segments':[{'segment_id':f"S{s['id']:06d}",'start_seconds':s['start'],'end_seconds':s['end'],'text':s['text']} for s in transcript['segments']],
            'available_frames':[f['timestamp_seconds'] for f in manifest]}
        observations=[]
        for path in sorted(directory.glob('visual-*.response.json')):
            if not re.fullmatch(r'visual-\d{3}\.response\.json',path.name):continue
            choice=read(path)['choices'][0]
            if choice.get('finish_reason')!='stop':raise ValueError('Visual receipt incomplete')
            observations.extend(json.loads(choice['message']['content'])['frames'])
        payload['visual_observations']=observations
        issues,files=review_candidate(directory,video,payload,[(f['timestamp_seconds'],directory/f['path']) for f in manifest],env)
        atomic(capture/'analysis-review-state.json',{'accepted':not issues,'issues':issues,
            'candidate_file':None,'correction_file':'user-correction.json','verification_files':files})
        if issues:
            jobs.save(vid,review_issues=issues)
            raise ReviewRejected(issues)
        analysis['review_provenance'].update(verification_files=files,separate_global_review=True)
        atomic(proof,{'correction_sha256':correction_hash,'analysis':analysis})
    info=read(directory/(vid+'.info.json'));report=finalize_analysis(capture,info,analysis)
    result=Publisher(report,delivery_title(info,analysis),'feishu',env=env).run()
    if result.get('status') not in {'delivered','already_delivered'}:
        raise ValueError('Corrected document not delivered')
    jobs.save(vid,status='complete',analysis_complete=True,report=str(report),delivery=result,
              human_correction_verified=True,review_issues=[],recovery_exhausted=False,error=None)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=pathlib.Path,required=True);p.add_argument('--video',required=True)
    p.add_argument('--repair',action='store_true');p.add_argument('--execute',action='store_true');a=p.parse_args()
    if not a.execute:raise SystemExit('Explicit --execute required for publication')
    jobs=Jobs(a.data/'jobs.sqlite')
    try:
        if a.repair:
            try:result=repair(jobs,a.video,a.data,os.environ)
            except Exception as error:
                job=jobs.get(a.video)
                if not job or job['status']=='complete':raise
                jobs.save(a.video,status='needs_review',review_issues=getattr(error,'issues',job['checkpoint'].get('review_issues',[])))
                result=handoff(jobs,a.video,a.data,os.environ)
        else:result=handoff(jobs,a.video,a.data,os.environ)
        from finance_alerts import flush
        print(json.dumps({'result':result,'alerts':flush(a.data,os.environ)},ensure_ascii=False))
    finally:jobs.db.close()


if __name__=='__main__':main()
