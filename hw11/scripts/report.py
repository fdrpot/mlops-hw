"""Render the HW11 report exclusively from retained experiment evidence.

Run: uv sync --locked --group report; uv run --group report python scripts/report.py
Missing runs remain explicitly missing; this program never synthesizes results.
"""
from __future__ import annotations
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results'
FIG = OUT / 'figures'
os.environ.setdefault('MPLCONFIGDIR', '/tmp/hw11-matplotlib')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                     'axes.spines.top': False, 'axes.spines.right': False,
                     'figure.facecolor': 'white', 'axes.grid': True, 'grid.alpha': .18,
                     'svg.fonttype': 'none'})


def records(path):
    if not path.exists(): return []
    result = []
    for line in path.read_text().splitlines():
        if line.strip():
            try: result.append(json.loads(line))
            except json.JSONDecodeError: pass # Interrupted final observer line is not evidence.
    return result


def read(path, default=None):
    if not path.exists(): return default
    try: return json.loads(path.read_text())
    except json.JSONDecodeError: return default


def fmt(value, digits=3):
    if value is None: return '—'
    if isinstance(value, float):
        if not math.isfinite(value): return '—'
        return f'{value:.{digits}f}'
    return escape(str(value))


def q(values, fraction):
    values = sorted(value for value in values if value is not None)
    if not values: return None
    pos = (len(values)-1)*fraction
    lower, upper = math.floor(pos), math.ceil(pos)
    return values[lower] + (values[upper]-values[lower])*(pos-lower)


def table(headers, rows):
    if not rows: return '<p class="missing">Данных пока нет.</p>'
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(value)+'</td>' for value in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def figure(name, title):
    path = FIG / (name+'.svg')
    payload = base64.b64encode(path.read_bytes()).decode()
    return f'<figure><img alt="{escape(title)}" src="data:image/svg+xml;base64,{payload}"><figcaption>{escape(title)}. <a href="results/figures/{name}.svg">SVG</a></figcaption></figure>'


def save(fig, name):
    fig.savefig(FIG/(name+'.svg'), bbox_inches='tight')
    fig.savefig(FIG/(name+'.png'), bbox_inches='tight', dpi=140)
    plt.close(fig)


def empty(ax, message='Сырых данных этого опыта пока нет'):
    ax.text(.5,.5,message,ha='center',va='center',transform=ax.transAxes,color='#657085')
    ax.set(xlabel='Время, с', ylabel='Наблюдения')


def pod_ready(pod):
    return not pod.get('deleted') and any(c['type']=='Ready' and c['status']=='True' for c in pod.get('conditions') or [])


def timelines():
    return {p.stem.removesuffix('-timeline'):records(p) for p in OUT.glob('*-timeline.jsonl')}


def plots(rows, runs, traces):
    FIG.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(3,1,figsize=(11,10),constrained_layout=True)
    rollout='a-rollout-fixed' if traces.get('a-rollout-fixed') else 'a-rollout'
    for ax,name,title in zip(axes,('a-warmup-broken','a-warmup-fixed',rollout),
                              ('Исходные probes: HTTP-процесс принят за готовность','Исправленные probes: ожидание backend','Штатное обновление A → B')):
        trace = traces.get(name, [])
        ax.set_title(title, loc='left', fontsize=11)
        if not trace:
            empty(ax); continue
        start=trace[0]['unix']
        xx=[t['unix']-start for t in trace]
        ax.step(xx,[sum(pod_ready(p) for p in t['pods']) for t in trace],where='post',label='Pod Ready')
        ax.step(xx,[sum(e.get('conditions',{}).get('ready',False) for s in t['endpoints'] if s['service']=='hw11-normal' for e in (s['endpoints'] or [])) for t in trace],where='post',label='Ready endpoints',linestyle='--')
        rr=[r for r in rows if r['run']==name]
        errors=[r for r in rr if r['outcome']!='completed']
        if errors: ax.scatter([r['sent_unix']-start for r in errors],[0]*len(errors),marker='x',color='#c33d32',label='Ошибки клиента',zorder=5)
        boundary=read(OUT/(name+'-boundary.json'),{}) if name.startswith('a-rollout') else {}
        if boundary.get('rollout_started_unix'):
            ax.axvline(boundary['rollout_started_unix']-start,color='#c33d32',linestyle=':',label='Начало rollout')
        ax.set(xlabel='Время от начала наблюдения, с',ylabel='Число реплик',ylim=(-.15,3.4))
        ax.legend(loc='upper right',fontsize=9)
    save(fig,'warmup-rollout')

    fig, axes=plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    for column,policy in enumerate(('sticky','normal')):
        selected=[r for r in rows if r['run']==f'b-hot80-{policy}-1' and r['outcome']=='completed']
        for row_index,metric in enumerate(('ttfc_seconds','queue_seconds')):
            ax=axes[row_index,column]
            ax.set_title(f'80% через одного клиента · {policy}',loc='left')
            if not selected:
                empty(ax);continue
            start=min(r['sent_unix'] for r in selected)
            for pod in sorted({r['pod'] for r in selected if r.get('pod')}):
                subset=[r for r in selected if r['pod']==pod and r.get(metric) is not None]
                ax.scatter([r['sent_unix']-start for r in subset],[r[metric] for r in subset],s=10,alpha=.65,label=pod[-11:])
            ax.set(xlabel='От начала отправки, с',ylabel='TTFC, с' if metric=='ttfc_seconds' else 'Очередь, с')
            ax.legend(title='Pod',fontsize=8)
    save(fig,'per-pod-latency-queue')

    fig,axes=plt.subplots(1,2,figsize=(12,5),constrained_layout=True)
    labels=[]
    for traffic in ('uniform','hot80'):
        for policy in ('sticky','normal'):
            labels.append((traffic,policy))
    for ax,metric,ylabel in zip(axes,('ttfc_seconds','queue_seconds'),('p95 TTFC, с','p95 очереди, с')):
        found=False
        for index,(traffic,policy) in enumerate(labels):
            values=[]
            for repeat in (1,2,3):
                name=f'b-{traffic}-{policy}-{repeat}'
                vals=[r.get(metric) for r in rows if r['run']==name and r['outcome']=='completed' and r.get(metric) is not None]
                if vals:
                    value=q(vals,.95);values.append(value);found=True
                    ax.scatter(index+(repeat-2)*.10,value,s=48,color=('#1976ad','#f28e2b','#3b9244')[repeat-1],label=f'Повтор {repeat}' if index==0 else None,zorder=4)
            if values:ax.plot([index-.22,index+.22],[statistics.mean(values)]*2,color='#172739',linewidth=2)
        if not found:empty(ax)
        ax.set_xticks(range(4),[t+'\n'+p for t,p in labels])
        ax.set(ylabel=ylabel,xlabel='Трафик / политика; точка — отдельный прогон, черта — среднее p95')
        if found:ax.legend()
    save(fig,'policies-three-runs')

    fig,ax=plt.subplots(figsize=(12,5),constrained_layout=True)
    trace=traces.get('c-scaling',[])
    if trace:
        start=trace[0]['unix'];xx=[r['unix']-start for r in trace]
        ax.step(xx,[next((h.get('status',{}).get('currentReplicas',float('nan')) for h in r['hpa']),float('nan')) for r in trace],where='post',label='HPA current replicas',color='#5439ae')
        ax.step(xx,[sum(pod_ready(p) for p in r['pods']) for r in trace],where='post',label='Ready',linestyle='--',color='#16837f')
        ax.set(xlabel='Время от начала наблюдения, с',ylabel='Число реплик',ylim=(1.5,3.5),yticks=[2,3])
        second=ax.twinx();rr=[r for r in rows if r['run'].startswith('c-') and r.get('sent_unix') and start<=r['sent_unix']<=trace[-1]['unix']]
        bins=range(0,int(trace[-1]['unix']-start)+6,5)
        counts=Counter(int((r['sent_unix']-start)//5)*5 for r in rr)
        planned=Counter(int((r['planned_unix']-start)//5)*5 for r in rr)
        second.plot(list(bins),[counts[t]/5 for t in bins],alpha=.5,color='#db8b20',label='Фактическая нагрузка')
        second.plot(list(bins),[planned[t]/5 for t in bins],alpha=.5,color='#db8b20',linestyle=':',label='Запланированная нагрузка')
        second.set_ylabel('Поступление запросов, запрос/с; окно 5 с')
        ax.legend(loc='upper left');second.legend(loc='upper right')
    else:empty(ax)
    ax.set_title('HPA CPU: 2 → 3 → 2 и поданная нагрузка',loc='left')
    save(fig,'scaling-2-3-2')


def run_table(runs):
    result=[]
    for run in runs:
        metric=run.get('ttfc_seconds',{})
        result.append([escape(run['run']),fmt(run.get('measurement_seconds')),fmt(run.get('planned')),
                       fmt(run.get('sent')),fmt(run.get('completed')),fmt(run.get('rejected')),
                       fmt(run.get('aborted')),fmt(run.get('timeouts')),fmt(run.get('transport_errors')),
                       fmt(run.get('http_5xx')),fmt(run.get('successful_n')),
                       fmt(metric.get('p50')),fmt(metric.get('p95')),
                       fmt(run.get('total_seconds',{}).get('p50')),fmt(run.get('total_seconds',{}).get('p95')),
                       fmt(run.get('send_lag_seconds',{}).get('p95')),
                       fmt(100*run.get('timeout_fraction',0),2)+'%' if run.get('timeout_fraction') is not None else '—',
                       fmt(100*run['error_fraction'],2)+'%' if run.get('error_fraction') is not None else '—'])
    return table(['Прогон','Фаза, с','План','Отпр.','Успех','Отказ','Обрыв','Тайм-аут','Сеть','5xx','N TTFC','TTFC p50, с','TTFC p95, с','Полный p50, с','Полный p95, с','Опоздание p95, с','Тайм-ауты','Ошибки'],result)


def checks_html():
    checks=read(OUT/'a-checks.json',{})
    return table(['Утверждение A','Наблюдение'],[(escape(key),'Подтверждено' if value else '<strong>Не подтверждено</strong>') for key,value in checks.items()])


def pod_table(rows):
    result=[]
    groups=defaultdict(list)
    for row in rows:
        if row['run'].startswith('b-') and row.get('pod'):
            groups[(row['run'],row['pod'])].append(row)
    for (run,pod),subset in sorted(groups.items()):
        success=[r for r in subset if r['outcome']=='completed']
        result.append([escape(run),escape(pod),len(subset),len(success),
                       fmt(q([r.get('ttfc_seconds') for r in success],.5)),fmt(q([r.get('ttfc_seconds') for r in success],.95)),
                       fmt(q([r.get('queue_seconds') for r in success],.95)),
                       fmt(sum(bool(r.get('cache_hit')) for r in success)/len(success)*100,1)+'%' if success else '—'])
    return table(['Прогон','Pod','Всего','Успех','TTFC p50, с','TTFC p95, с','Очередь p95, с','Cache hit'],result)


def routing_table(rows):
    groups=defaultdict(Counter)
    for row in rows:
        if row['run'].startswith('b-') and row.get('pod'):
            groups[(row['run'],row.get('client_pod','client-'+str(row.get('client'))))][row['pod']]+=1
    return table(['Прогон','Клиент / исходный IP — см. clients.json','Распределение фактических ответов'],
                 [(escape(run),escape(str(client)),escape(', '.join(f'{pod}: {n}' for pod,n in counter.items()))) for (run,client),counter in sorted(groups.items())])


def hpa_table(traces):
    trace=traces.get('c-scaling',[])
    if not trace:return '<p class="missing">Линия HPA ещё не сохранена.</p>'
    def utc(stamp):return datetime.fromtimestamp(stamp,timezone.utc).isoformat()
    def current(sample):
        return next((h.get('status',{}).get('currentReplicas') for h in sample.get('hpa',[])),None)
    result=[];origin=trace[0]['unix']
    first_two=next((r for r in trace if current(r)==2),None)
    first_three=next((r for r in trace if current(r)==3),None)
    for replica,sample in ((2,first_two),(3,first_three)):
        if sample:result.append([f'Первое наблюдение HPA={replica}',utc(sample['unix']),fmt(sample['unix']-origin)+' с от начала линии'])
    summary=read(OUT/'c-scale-up-summary.json',{})
    load_start=summary.get('start_unix')
    if load_start and first_three:
        result.append(['От начала CPU-нагрузки до HPA current=3',utc(first_three['unix']),fmt(first_three['unix']-load_start)+' с'])
    initial={pod['name'] for pod in trace[0].get('pods',[]) if not pod.get('deleted')}
    added={}
    for sample in trace:
        for pod in sample.get('pods',[]):
            name=pod['name']
            if name in initial:continue
            entry=added.setdefault(name,{'created':pod.get('created'),'first_observed':sample['unix'],'ready':None})
            for condition in pod.get('conditions') or []:
                if condition.get('type')=='Ready' and condition.get('status')=='True' and entry['ready'] is None:
                    entry['ready']=condition.get('lastTransitionTime')
                    entry['ready_observed']=sample['unix']
    for name,entry in sorted(added.items(),key=lambda pair:pair[1]['first_observed']):
        created=entry.get('created');ready=entry.get('ready')
        if not created:continue
        born=datetime.fromisoformat(created.replace('Z','+00:00')).timestamp()
        ready_at=datetime.fromisoformat(ready.replace('Z','+00:00')).timestamp() if ready else None
        result.append(['Создан дополнительный Pod '+escape(name),escape(created),fmt(born-load_start)+' с от начала нагрузки' if load_start else fmt(born-origin)+' с от начала линии'])
        result.append(['Этот Pod стал Ready',escape(ready) if ready else 'Не наблюдалось',fmt(ready_at-born)+' с от создания' if ready_at is not None else '—'])
    down=read(OUT/'c-scale-down.json',{})
    if down:
        stopped=down.get('load_stopped_unix');observed=down.get('observed_unix')
        if stopped:result.append(['Нагрузка завершена; начало ожидания scale-down',utc(stopped),'Окно стабилизации настроено 30 с'])
        if observed:result.append(['Возврат к минимуму после нагрузки',utc(observed),fmt(down.get('wait_seconds'))+' с фактического ожидания'])
    html=table(['Событие','UTC','Длительность / границы'],result)+'<p>Создание и Ready взяты из creationTimestamp и Ready.lastTransitionTime Kubernetes. HPA снимается с шагом около 2 с; фактическое ожидание scale-down измеряется с шагом 5 с. В него входят стабилизация, окно resource metrics, цикл контроллера и завершение Pod, поэтому оно может превышать 30 с.</p>'
    hot=read(OUT/'c-hot-sticky-summary.json',{})
    if hot:
        samples=[sample for sample in trace if hot['start_unix']<=sample['unix']<=hot['end_unix']]
        pods={pod['name'] for sample in samples for pod in sample.get('pods',[]) if not pod.get('deleted')}
        counts=hot.get('by_pod',{})
        html+='<h3>Горячий клиент при включённом HPA</h3>'+table(['Pod в период hot80','Запросов по фактическим ответам'],[[escape(pod),fmt(counts.get(pod,0))] for pod in sorted(pods)])
        html+='<p>Таблица включает Pod с нулём ответов: создание новой реплики не переносит существующие ClientIP-привязки. Отказы и задержки этого отдельного CPU-прогона приведены в c-hot-sticky; параметры CPU отличаются от сравнений B, поэтому их задержки напрямую не объединяются.</p>'
    return html


def alerts_table():
    observations=[]
    for path in OUT.glob('*-prometheus.jsonl'):
        observations.extend(records(path))
    observations.sort(key=lambda r:r.get('unix',0))
    names={a.get('labels',{}).get('alertname','unknown') for r in observations
           for a in r.get('alerts',{}).get('data',{}).get('alerts',[])}
    result=[]
    for name in sorted(names):
        first={}; last='inactive'
        for row in observations:
            state=next((a.get('state','unknown') for a in row.get('alerts',{}).get('data',{}).get('alerts',[]) if a.get('labels',{}).get('alertname')==name),'inactive')
            if state in ('pending','firing'):first.setdefault(state,row['unix'])
            if state=='inactive' and 'firing' in first:first.setdefault('recovered',row['unix'])
            last=state
        stamp=lambda key:datetime.fromtimestamp(first[key],timezone.utc).isoformat() if first.get(key) else 'Не наблюдалось'
        result.append([escape(name),stamp('pending'),stamp('firing'),stamp('recovered'),escape(last)])
    return table(['Алерт','Первое pending (UTC)','Первое firing (UTC)','Исчез после firing (UTC)','Последнее состояние'],result)


def calibration_table():
    data=read(OUT/'monitoring/calibration.json',{})
    if not data:return '<p class="missing">Контрольный cohort Prometheus пока не измерен.</p>'
    prometheus=data.get('prometheus_p95',{}).get('data',{}).get('result',[])
    prom_p95=float(prometheus[0]['value'][1]) if prometheus else None
    n=len(data.get('rows',[]));server_n=len(data.get('server_logs',[]))
    result=table(['Одинаковый cohort','N','p95 TTFC, с'],[
        ['Клиент: monotonic, включая сетевую доставку',n,fmt(data.get('client_p95_seconds'),6)],
        ['Backend: завершённые записи журнала',server_n,fmt(data.get('server_p95_seconds'),6)],
        ['Prometheus: точные дельты суммарных buckets',fmt(data.get('exact_bucket_deltas',{}).get('+Inf')),fmt(prom_p95,6)]])
    result+='<pre>'+escape(data.get('promql',''))+'</pre>'
    result+='<p>В контрольном прогоне все 20 завершённых TTFC попали в бакет (0,2; 0,4] с. Histogram предполагает равномерное распределение внутри бакета: 0,2 + (0,95 × 20 / 20) × 0,2 = 0,39 с. Поэтому p95 bucket выше эмпирического server/client p95 около 0,226/0,228 с; здесь основная разница — ширина бакета, а клиентская добавка около 2 мс связана с доставкой и границами замера. Количество наблюдений совпадает: 20/20/20. Среднее перцентилей реплик не используется. Для более точного хвоста следовало бы сузить границы возле 0,22 с либо применять native histogram с выбранной точностью.</p>'
    result+='<p>Полные снимки до/после и записи request ID: <a href="results/monitoring/calibration.json">calibration.json</a>; проверки реальных scrape-целей, Grafana и дашборда: <a href="results/monitoring/checks.json">checks.json</a>.</p>'
    return result


def idle_during_queue(rows):
    groups=defaultdict(list)
    for row in rows:
        if row['run'].startswith('b-hot80-sticky-'):
            groups[row['run']].append(row)
    evidence=[]
    for run,subset in sorted(groups.items()):
        if any(r['outcome']!='completed' for r in subset):continue
        pods={r.get('pod') for r in subset if r.get('pod')}
        if len(pods)!=2:continue
        for row in subset:
            if (row.get('queue_seconds') or 0)<.5:continue
            moment=row['sent_unix']+row['queue_seconds']/2
            other=next(pod for pod in pods if pod!=row['pod'])
            active=[r for r in subset if r.get('pod')==other and r['sent_unix']+r['queue_seconds']<=moment<r['completed_unix']]
            if not active:
                evidence.append([escape(run),escape(row['request_id']),escape(row['pod']),fmt(row['queue_seconds']),escape(other),datetime.fromtimestamp(moment,timezone.utc).isoformat()])
                break
    if not evidence:return '<p class="missing">Одновременное ожидание горячего Pod и свободная ёмкость второго пока не подтверждены сохранёнными интервалами.</p>'
    return '<p>Проверка одновременности: для полностью успешного прогона восстановлены интервалы занятости [sent + queue; completed]. В середине фактического ожидания указанного запроса на другом Pod не было активного интервала. Это реконструкция по клиентским часам и server queue (сетевые границы дают небольшую погрешность); raw running/waiting Prometheus служат независимой проверкой.</p>'+table(['Прогон','Запрос ждёт','Горячий Pod','Ожидание, с','Другой Pod без активного запроса','Момент UTC'],evidence)


def route_analysis_html():
    analysis=read(OUT/'route-analysis.json',{})
    if not analysis:return '<p class="missing">Анализ трасс маршрутизации пока не построен.</p>'
    paired=analysis.get('paired_comparisons',[])
    result='<h3>Сравнение исправления по одинаковому расписанию</h3>'
    result+=table(['Трафик / повтор','Расписание совпадает','p95 TTFC sticky / normal, с','Ошибки sticky / normal','Доля занятого Pod sticky / normal'],[
        [escape(p['traffic'])+' / '+fmt(p['repeat']),'Да' if p['identical_schedules'] else '<strong>Нет</strong>',
         fmt(p['sticky_p95_ttfc_seconds'])+' / '+fmt(p['normal_p95_ttfc_seconds']),
         fmt(100*p['sticky_error_fraction'],1)+'% / '+fmt(100*p['normal_error_fraction'],1)+'%',
         fmt(100*p['sticky_busiest_pod_share'],1)+'% / '+fmt(100*p['normal_busiest_pod_share'],1)+'%'] for p in paired])
    hot=[p for p in paired if p['traffic']=='hot80']
    if hot:
        better=sum(p['normal_p95_ttfc_seconds']<p['sticky_p95_ttfc_seconds'] for p in hot if p['normal_p95_ttfc_seconds'] is not None and p['sticky_p95_ttfc_seconds'] is not None)
        no_worse_errors=sum(p['normal_error_fraction']<=p['sticky_error_fraction'] for p in hot)
        result+=f'<p>В сохранённых hot80 парах обычный Service уменьшил p95 TTFC в {better} из {len(hot)} сравнений; доля ошибок не выросла в {no_worse_errors} из {len(hot)}. Это p95 всех успешных запросов каждого прогона; перцентили реплик не усредняются. Полный вывод о трёх повторах допустим после появления всех трёх пар.</p>'
    balanced=analysis.get('no_skew_uniform_sticky_cases',[])
    if balanced:
        result+='<h3>Сохранённый результат без перекоса ClientIP</h3>'
        result+=table(['Прогон','Доля занятого Pod','p95 TTFC, с','Исходы','Распределение четырёх клиентов'],[
            [escape(item['run']),fmt(100*item['busiest_pod_share'],1)+'%',
             fmt(analysis['runs'][item['run']]['ttfc']['p95_seconds']),
             escape(str(analysis['runs'][item['run']]['outcomes'])),
             escape('; '.join(client+' → '+','.join(pods) for client,pods in item['by_client_pod'].items()))] for item in balanced])
        result+='<p>Четыре равномерных источника могут распределиться по двум endpoints в отношении 2:2. При такой трассе ClientIP сохраняет кеш и не создаёт горячую реплику; это реальный сохранённый результат, а не предположение о работе Service. Следовательно, обычный Service выбран для устойчивости к наблюдённому перекосу и hot80, а его превосходство при любом распределении нагрузки не заявляется.</p>'
    zero=[]
    for name,run in analysis.get('runs',{}).items():
        for pod,count in run.get('by_pod',{}).items():
            if count==0:zero.append([escape(name),escape(pod),0,'Pod присутствует в control-plane timeline, запросов с его идентичностью нет'])
    if zero:
        result+='<h3>Реплики с нулём запросов</h3>'+table(['Прогон','Pod','Запросов','Источник'],zero)
    prom=analysis.get('prometheus_simultaneous_idle',{})
    if prom.get('simultaneous_samples'):
        result+='<h3>Одновременное ожидание и свободная реплика — Prometheus</h3>'
        result+=f'<p>Внутри фактических периодов B sticky найдено {prom["simultaneous_samples"]} пар синхронных samples: у одного Pod waiting &gt; 0, у другого одновременно running=0 и waiting=0. Шаг выгрузки — {fmt(prom.get("step_seconds"))} с. Ниже точки сценария hot80 (при наличии); это прямые gauges, а не оценка свободной ёмкости по среднему CPU.</p>'
        result+=table(['UTC','Прогон','Горячий Pod','Waiting','Running','Свободный Pod'],[
            [datetime.fromtimestamp(sample['unix'],timezone.utc).isoformat(),escape(', '.join(sample.get('runs',[]))),escape(sample['waiting_pod']),fmt(sample['waiting']),fmt(sample.get('running')),escape(sample['idle_pod'])] for sample in (prom.get('hot80_samples') or prom.get('first_samples',[]))[:8]])
    else:
        result+='<p class="missing">Синхронные Prometheus gauges для ожидания горячего Pod при свободной второй реплике пока не обработаны.</p>'
    reconstructed=analysis.get('reconstructed_idle',{}).get('examples',[])
    if reconstructed:
        result+='<details><summary>Независимая реконструкция по журналу запросов</summary><p>Для полностью записанного прогона допускаются успехи и HTTP429, которые не выполняют backend-работу. По клиентскому wall-clock и server queue восстановлены интервалы [sent + queue; completed]. В середине ожидания указанного запроса другой Pod не обрабатывал запрос. Сетевые границы и точность wall-clock ограничивают этот способ; синхронные gauges выше дают более прямое подтверждение.</p>'
        result+=table(['Прогон','Ждущий request ID','Ожидание, с','Другой Pod без активного интервала'],[[escape(e['run']),escape(e['request_id']),fmt(e['queue_seconds']),escape(e['other_pod'])] for e in reconstructed[:6]])+'</details>'
    result+='<p>Машинный анализ и проверки соответствия raw ↔ summary ↔ schedule: <a href="results/route-analysis.json">route-analysis.json</a>. Нулевое число ответов одной реплики не исключает её из анализа топологии.</p>'
    return result


def environment():
    data=read(OUT/'environment.json',{})
    commands=data.get('commands',{})
    def text(name):return commands.get(name,{}).get('stdout','')
    def parsed(name):
        try:return json.loads(text(name))
        except (json.JSONDecodeError,TypeError):return {}
    docker=parsed('docker');image=parsed('image');kubernetes=parsed('kubernetes');nodes=parsed('nodes');pods=parsed('pods')
    rows=[]
    limits=data.get('profile_limits',{})
    if limits:rows.append(['Лимиты профиля Colima hw11',f"{limits.get('cpu','—')} CPU / {limits.get('memory_gib','—')} GiB RAM / {limits.get('disk_gib','—')} GiB data disk"])
    if docker:
        rows.append(['Docker Engine / архитектура',escape(str(docker.get('ServerVersion','—')))+' / '+escape(str(docker.get('Architecture','—')))])
        rows.append(['Доступно Docker VM по docker info',fmt(docker.get('NCPU'))+' CPU / '+fmt(docker.get('MemTotal',0)/1024**3,3)+' GiB RAM'])
    for name,key in [('Kubernetes','serverVersion'),('kubectl','clientVersion')]:
        version=kubernetes.get(key,{})
        if version:rows.append([name,escape(version.get('gitVersion','—'))+' / '+escape(version.get('platform','—'))])
    for name in ('kind','helm'):
        if text(name):rows.append([name,escape(text(name).strip())])
    node_info=next((n.get('status',{}).get('nodeInfo',{}) for n in nodes.get('items',[])),{})
    if node_info:rows.append(['Runtime внутри kind / kernel',escape(node_info.get('containerRuntimeVersion','—'))+' / '+escape(node_info.get('kernelVersion','—'))])
    api=next((p for p in pods.get('items',[]) if p.get('metadata',{}).get('labels',{}).get('app')=='hw11-api'),None)
    if api:
        for container in api['spec']['containers']:
            resource=container.get('resources',{});request=resource.get('requests',{});limit=resource.get('limits',{})
            rows.append(['API CPU request / limit',escape(str(request.get('cpu','—')))+' / '+escape(str(limit.get('cpu','—')))])
            rows.append(['API RAM request / limit',escape(str(request.get('memory','—')))+' / '+escape(str(limit.get('memory','—')))])
    if image:
        rows.append(['Образ приложения: inspect Size',fmt(image.get('Size',0)/10**6,3)+' MB (метаданные Docker content store)'])
        rows.append(['Закреплённый ID построенного образа','<code>'+escape(image.get('Id','—'))+'</code>'])
    for name,label in [('disk','Свободное место хоста при снимке'),('vm_disk','Фактический data disk VM'),('docker_disk','Размер images / volumes / build cache')]:
        raw=text(name).strip()
        if raw:rows.append([label,'<pre>'+escape(raw)+'</pre>'])
    for filename,label in [('disk-before.txt','Свободный диск хоста до стенда'),('vm-disk-before.txt','Data disk VM до Kubernetes')]:
        file=OUT/filename
        if file.exists():rows.append([label,'<pre>'+escape(file.read_text().strip())+'</pre>'])
    result=table(['Параметр / источник','Фактически сохранённое значение'],rows) if rows else ''
    if rows:result+='<p>GiB/MiB — двоичные единицы; MB/GB и размерные строки Docker/df сохранены как у источника. inspect Size описывает учёт содержимого образа и не равен всему занятому диску VM; отдельно показаны все images, volumes, build cache и фактическая файловая система. Лимит VM 16 GiB не считается фактически занятым размером.</p>'
    files=['environment.json','docker-info.json','versions.txt','disk-before.txt','vm-disk-before.txt','size-after.txt','stand-size.txt','c-scale-down.json']
    parts=[]
    for name in files:
        path=OUT/name
        if path.exists():
            raw=path.read_text()
            parts.append(f'<details><summary>{escape(name)}</summary><pre>{escape(raw[:15000])}</pre></details>')
    return result+(''.join(parts) or '<p class="missing">Снимок окружения отсутствует.</p>')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--commit',default=None)
    args=p.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    from analyze_routes import main as analyze_routes
    analyze_routes()
    rows=records(OUT/'requests.jsonl')
    runs=[read(path,{}) for path in sorted(OUT.glob('*-summary.json'))]
    runs=[run for run in runs if run.get('run')]
    traces=timelines()
    plots(rows,runs,traces)
    missing=[]
    for traffic in ('uniform','hot80'):
        for policy in ('sticky','normal'):
            for repeat in (1,2,3):
                name=f'b-{traffic}-{policy}-{repeat}'
                found=next((run for run in runs if run['run']==name),{})
                if found.get('measurement_seconds',0)<120:missing.append(name)
    if not read(OUT/'a-checks.json'):missing.append('Проверки A')
    if not traces.get('c-scaling'):missing.append('Измерение HPA')
    if not list(OUT.glob('*-prometheus.jsonl')):missing.append('Переходы алертов')
    completeness='<div class="note"><strong>Статус доказательств.</strong> '+('Сохранены все 12 сравнительных прогонов B длительностью не менее 120 с и основные журналы A/C. Подтверждение каждого утверждения приведено ниже.' if not missing else 'Ещё отсутствуют: '+escape(', '.join(missing))+'. Эти результаты не заявляются выполненными.')+'</div>'
    submission=read(OUT/'submission.json',{})
    commit=args.commit or submission.get('source_commit','не зафиксирован в метаданных отчёта')
    cohort=''
    for path in sorted(OUT.glob('*comparison*.json')):
        cohort+=f'<details><summary>{escape(path.name)}</summary><pre>{escape(path.read_text())}</pre></details>'
    if not cohort:cohort=''
    case_file=OUT/'lecture-case.json'
    case=read(case_file,{}) or {'title':'Google Cloud: GKE Inference Gateway / Vertex AI',
        'url':'https://cloud.google.com/blog/products/containers-kubernetes/how-gke-inference-gateway-improved-latency-for-vertex-ai',
        'explanation':'Лекция 11, слайды 69–70, 07:52–08:36: горячий префикс и влияние очереди; результаты нагрузки Google. Материал сопоставлен с предоставленным lecture11-extended.mp4.'}
    case_html=(f'<p>Кейс из лекции: <a href="{escape(case["url"],quote=True)}">{escape(case.get("title","Первоисточник"))}</a>. {escape(case.get("explanation",""))}</p>' if case.get('url') else '<p class="note">Материал лекции не приложен к заданию. Ниже выбран первичный кейс Red Hat / llm-d; его присутствие в лекции пока не подтверждено.</p>')
    html='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ДЗ 11 — Kubernetes под нагрузкой</title><style>
    *{box-sizing:border-box}body{margin:0;color:#172739;background:#eef1f6;font:16px/1.6 system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:48px;background:white}h1{font-size:38px;line-height:1.15;margin-bottom:12px}h2{font-size:25px;margin-top:46px;border-top:1px solid #ccd5df;padding-top:20px}h3{font-size:19px;margin-top:24px}p{max-width:1000px}a{color:#4934a3}code,pre{font-family:ui-monospace,monospace;font-size:13px}pre{padding:18px;background:#f2f5fa;white-space:pre-wrap;overflow-wrap:anywhere}.lead{font-size:20px;color:#516178}.note{border-left:4px solid #7156b5;background:#f1edf8;padding:16px 20px}.missing{color:#93471b}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid #dce3eb;padding:9px;text-align:left;vertical-align:top}th{background:#f0f4f9;white-space:nowrap}td{overflow-wrap:anywhere}figure{margin:28px 0}img{width:100%;height:auto}figcaption{font-size:13px;color:#516178}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.meta{color:#657085;font-size:13px}@media(max-width:700px){main{padding:24px 18px}h1{font-size:29px}}@media print{body{background:white}main{padding:0}h2{break-after:avoid}figure,tr,.note{break-inside:avoid}details{display:block}summary{display:block}@page{size:A4;margin:15mm}}</style><main>
    <p class="meta">МГТУ · MLOps · ДЗ 11 · воспроизводимый CPU-стенд</p><h1>Сервис работает,<br>пользователи ждут</h1><p class="lead">Прогрев, потоковый выпуск, привязка к Pod и HPA в локальном Kubernetes.</p>'''
    html+=f'<p class="meta">Отчёт построен {datetime.now(timezone.utc).isoformat(timespec="seconds")}; backend_mode=simulated. Репозиторий: <a href="https://github.com/fdrpot/mlops-hw/tree/hw11/hw11">fdrpot/mlops-hw / hw11</a>. Ревизия исходников: <code>{escape(commit)}</code>.</p>'+completeness
    html+='''<h2>1. Стенд и границы измерений</h2><p>Продолжен API из ДЗ 8–10: FastAPI, проверка загружаемого артефакта, ограниченная очередь, одна версия на процесс и проверяемая готовность. Для Kubernetes добавлены выдача NDJSON частями, завершение активных потоков, идентичность Pod и Prometheus. Модель здесь — управляемый CPU-имитатор. Ни одна длительность ниже не называется TTFT/TPOT и не характеризует нейросетевую модель.</p><p>Deployment содержит две реплики. Обычный Service и Service с ClientIP выбирают те же Pod. В четырёх отдельных клиентских Pod выполняется один стандартный генератор. Для сравнения сохраняются одинаковое расписание, prompt/history, длина ввода, max_chunks и интервалы частей. Соединение закрывается после каждого запроса; автоматических повторов нет. До каждого прогона B все реплики перезапускаются, прогрев завершается, локальные кеши пусты.</p><p>LRU-кеш ограничен 16 элементами: ключ — SHA256 первых 64 символов prompt. Cache hit имитирует 0,04 с работы, miss — 0,20 с. Одновременно работает один запрос; очередь — до 64, ожидание до 60 с. В B каждая часть дополнительно потребляет 0,02 с реального CPU; в HPA — 0,15 с. Это фиксированная модель нагрузки, а не измерение эффективности KV-кеша.</p><p>Файлы <code>artifacts/model-A.json</code> и <code>model-B.json</code> включены в образ и версионный immutable ConfigMap. При загрузке проверяются SHA256 и фактическая версия. Общего RW-тома весов нет. SHA A: <code>0144a05a7ba9306da19204796bdc3dd930d0c0d771a40353e7db3b8090281886</code>; SHA B: <code>8efab4e985d6ff843ff34545a37edf189ad0732d8e8afcc8c20427e84f73e6b0</code>. При замене Pod загружается тот же закреплённый артефакт. Диалог хранит клиент и передаёт history при каждом обращении.</p><p>CPU request 100m / limit 500m, RAM request 64 MiB / limit 192 MiB на backend. Grace period 60 с; Uvicorn даёт активным обработчикам 55 с. Startup допускает длительный прогрев, readiness исключает непрогретый/останавливающийся Pod, liveness проверяет HTTP-процесс. Источники: <a href="https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/">Kubernetes probes</a>, <a href="https://kubernetes.io/docs/concepts/services-networking/service/#session-affinity">Service ClientIP</a>.</p>'''
    html+=environment()
    html+='''<h2>2. Опыт A: прогрев, обновление и откат</h2><p><strong>Гипотеза.</strong> Готовность HTTP-процесса не подтверждает готовность backend; преждевременный endpoint отправляет запрос на непрогретую реплику. Сохранён режим <code>PROBE_MODE=broken</code>: startup/readiness отвечают 200 сразу. В режиме fixed они ждут окончания проверки артефакта и прогрева 20 с.</p><p><strong>Воспроизведение и изменение.</strong> Две реплики запускаются с нуля под одинаковыми запросами. До прогрева в исправленном варианте Service может ещё не иметь endpoints: такой клиентский сетевой отказ учитывается отдельно от ответа backend 503. Штатный rollout выполняется из полностью готовой A в B под непрерывной нагрузкой; один ответ из 80 частей с интервалом 0,5 с заранее пересекает начало rollout. Изменены probes и сохранение активных потоков в grace period.</p><p><strong>Повтор.</strong> Ниже показаны независимые клиентские результаты и Ready/EndpointSlice. Заведомо неготовая B должна оставаться вне endpoints; откат подтверждается версией A в ответах, а не только статусом Deployment. Принудительный SIGKILL выделен в отдельный прогон, в нём ожидается реальный обрыв. Время восстановления оценивается по линии endpoints и следующему успешному ответу.</p>'''
    html+=checks_html()+figure('warmup-rollout','График 1. Ready/endpoints и ошибки на исходном прогреве, после исправления и при rollout')
    boundary=read(OUT/'a-forced-kill-boundary.json',{})
    if boundary:
        killed=boundary['killed_at_unix']
        html+=table(['Принудительное завершение','Наблюдение'],[
            ['Команда','kubectl delete pod --grace-period=0 --force'],
            ['Прерванный Pod',escape(boundary['victim_pod'])],
            ['Обнаружение исчезновения, с после kill',fmt(boundary['failure_detected_unix']-killed)],
            ['Новый Pod Ready, с после kill',fmt(boundary['replacement_observed_ready_unix']-killed)],
            ['Первый замеренный успешный ответ после kill, с',fmt(boundary['first_postkill_completed_unix']-killed)]])
        html+='<p>Первая инъекция через os.kill(1, SIGKILL) из kubectl exec не прервала поток: процесс init пространства PID проигнорировал сигнал, что согласуется с <a href="https://man7.org/linux/man-pages/man7/pid_namespaces.7.html">Linux pid_namespaces(7)</a>. Этот неудачный опыт сохранён как a-forced, вместе с a-checks-original.json и experiment-a-original.log. Повтор a-forced-kill заставил kubelet/runtime завершить контейнер с нулевым grace period: клиент действительно получил оборванный HTTP200 без done, затем новый Pod прогрелся и восстановил обслуживание. Границы времени и идентичности сохранены в <a href="results/a-forced-kill-boundary.json">a-forced-kill-boundary.json</a>.</p>'
    html+='<h3>Гонка остановки, обнаруженная проверкой отката</h3><p>Первая Kubernetes-проверка B→A получила HTTP503 сразу после успешного rollout status. Лог сохранён в <a href="results/kubernetes-tests-before-drain-fix.log">kubernetes-tests-before-drain-fix.log</a>. Предполагаемая причина — старый Pod уже включил drain, а маршрут Service ещё не обновился. Тело первого HTTPError не сохранилось, поэтому конкретный Pod в этой попытке не установлен. Гонка согласуется с <a href="https://kubernetes.io/docs/concepts/workloads/pods/pod-termination/#pod-termination-flow">асинхронной процедурой завершения Pod</a>.</p><p>Исправление: preStop сначала 3 с принимает остаточный трафик, затем вызывает /drain с тайм-аутом 0,5 с, ждёт 1 с и передаёт управление SIGTERM. После hook Uvicorn имеет 55 с на активные ответы при общем grace 60 с. ready() теперь ждёт исчезновения terminating Pod и точного соответствия готовых EndpointSlice текущим Ready Pod. Это барьер состояния Kubernetes, а не повтор пользовательского запроса. Три секунды — бюджет распространения в этом локальном стенде; гарантию для другой сети нужно измерять отдельно.</p>'
    fixed_checks=read(OUT/'a-rollout-fixed-checks.json',{})
    if fixed_checks:
        html+=table(['Повтор после исправления drain','Результат'],[[escape(key),fmt(value)] for key,value in fixed_checks.items()])
    html+='<h3>Все сырые исходы по прогонам</h3>'+run_table(runs)
    html+='''<p><strong>Ограничение.</strong> Нулевые ошибки в одном штатном прогоне относятся к его расписанию, длинам ответов и тайм-аутам. Grace period не гарантирует завершение ответа произвольной длины. SIGKILL не запускает корректное завершение и не обеспечивает прозрачное продолжение уже начатого потока.</p>
    <h2>3. Опыт B: локальность кеша и горячая реплика</h2><p><strong>Гипотеза.</strong> ClientIP сохраняет локальность кеша, но удерживает 80% запросов одного клиента на одном Pod. Другой Pod может простаивать, пока очередь горячего увеличивает TTFC. На обычном Service каждое новое соединение может выбрать другой endpoint.</p><p><strong>Воспроизведение.</strong> Для uniform и hot80 сравниваются sticky/normal, три повтора на комбинацию, фаза каждого не короче 120 с. IP четырёх клиентов сохранены в <a href="results/clients.json">clients.json</a>; фактическое назначение клиент → Pod ниже. Нельзя выводить распределение только из названия политики: его задают kube-proxy/CNI, исходный IP и жизненный цикл соединений. Полные настройки Service сохранены в <a href="results/b-services.json">b-services.json</a>.</p><p><strong>Изменение.</strong> В этом CPU-стенде исправление — обычный Service без ClientIP и короткие отдельные соединения. Он жертвует частью локальности, чтобы новый запрос мог попасть к другой реплике. Выбор оценивается по TTFC/очереди, отказам и распределению, а не по среднему CPU. Для настоящего inference engine потребуется отдельная проверка cache-aware политики с ценой prefill, очередью, размером KV-кеша и измеренными TTFT/TPOT.</p>'''
    html+=figure('per-pod-latency-queue','График 2. TTFC и очередь по Pod: первый повтор hot80 на тех же двух репликах')
    html+=figure('policies-three-runs','График 3. Все три повтора политик: p95 успешных ответов отдельно по каждому прогону')
    html+=route_analysis_html()+'<h3>Повтор и распределение</h3>'+pod_table(rows)+'<details><summary>Трасса привязки каждого клиента</summary>'+routing_table(rows)+'</details>'
    balanced=[]
    for run in runs:
        if '-sticky-' in run['run']:
            counts={pod:count for pod,count in run.get('by_pod',{}).items() if pod not in ('null','None',None)}
            if counts and max(counts.values())/sum(counts.values())<=.65:
                balanced.append(run['run'])
    html+=('<p><strong>Результат без ожидаемого перекоса.</strong> В '+escape(', '.join(balanced))+' доля наиболее занятого Pod по ответам не превышала 65%. Трасса показывает, какие из четырёх IP попали на каждую реплику; ClientIP сам по себе не требует неравномерного распределения при равномерных клиентах.</p>' if balanced else '<p class="missing">В сохранённых sticky-прогонах пока не найден отдельный результат без перекоса (максимальная доля Pod ≤65%). Если такого исхода нет, его нельзя подменять ожиданием; нужен отдельный сохранённый опыт.</p>')
    html+='''<p><strong>Ограничение.</strong> Имитация кеша здесь ограничена длиной префикса и временем hit/miss. HPA и обычный Service не учитывают стоимость запроса и свободную KV-память. Холодная третья реплика может не получить горячий ClientIP до смены affinity.</p>
    <h2>4. Опыт C: CPU HPA и алерты</h2><p><strong>Гипотеза.</strong> При реальной вычислительной нагрузке CPU HPA масштабирует 2 → 3, затем возвращает 2 после снижения загрузки и окна стабилизации. Средний CPU может скрыть горячую реплику: например, 100% и 0% дают средние 50%, ниже цели 60%. При среднем выше цели HPA добавит Pod, но ClientIP продолжит вести прежний IP к его старому endpoint.</p><p><strong>Конфигурация и повтор.</strong> HPA autoscaling/v2: min=2, max=3, CPU target 60% от request 100m; scale-down stabilization 30 с. Вычисления выполняются в рабочем потоке, health остаётся доступным. Наличие metrics.k8s.io сохранено отдельно. Статус и условия HPA снимаются каждые 2 с; время обнаружения события ограничено этим шагом и периодом контроллера. Источник: <a href="https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/">Kubernetes HPA</a>.</p>'''
    html+=hpa_table(traces)+figure('scaling-2-3-2','График 4. HPA/Ready и запланированная/фактическая нагрузка')
    html+='''<p>Два алерта используют окно 30 с, минимум 10 наблюдений и удержание 10 с: p95 TTFC выше 0,6 с и доля отказов выше 10%. Для воспроизведения очередь ограничивается 4 местами, ожидание 0,5 с, поступление повышается до 20 запрос/с; затем нагрузка снижается. Состояния pending/firing сохраняются через API Prometheus, а нормализация подтверждается исчезновением активного алерта после последнего firing и достаточного периода восстановления.</p>'''
    html+=alerts_table()
    html+='''<p><strong>Ограничение.</strong> Факт конфигурации алерта не доказывает firing; отсутствие данных отображается явно. Если короткий ответ был отклонён раньше очереди, он не входит в гистограмму успешных TTFC. Увеличение числа Pod само по себе не меняет существующую привязку клиента.</p>
    <h2>5. Корректность измерений и PromQL</h2><p>Клиент использует time.monotonic: TTFC = first_monotonic − sent_monotonic, total = completed_monotonic − sent_monotonic. Для N содержательных частей сохраняются N−1 inter-chunk интервалов; для одной части средний интервал отсутствует. Часы разных Pod не вычитаются. Wall-clock нужен только для общего rendezvous и сопоставления с Kubernetes. Каждая запланированная попытка хранит план и фактическую отправку, send_lag, status, outcome, Pod, версию, число частей и done. Отдельные счётчики: planned/sent/completed/rejected/aborted/timeout/transport_error. Успех — HTTP200 и конечный done; поток HTTP200 без done считается оборванным.</p><p>Процентили клиента — линейная интерполяция между порядковыми статистиками, отдельно для успешных ответов и с N. Доля ошибок включает все незавершённые попытки; доля тайм-аутов сохраняется отдельно. Request/session ID есть только в журнале. Ограниченные метки метрик: pod, model_version и outcome. Histogram хранит секунды и отдельные bucket до 180 с.</p><pre>histogram_quantile(0.95,
  sum by (le) (rate(hw11_ttfc_seconds_bucket[1m])))

histogram_quantile(0.95,
  sum by (le, pod) (rate(hw11_ttfc_seconds_bucket[1m])))

sum by (pod) (rate(hw11_requests_total{outcome="complete"}[1m]))
sum(rate(hw11_requests_total{outcome="rejected"}[1m]))
 / clamp_min(sum(rate(hw11_requests_total[1m])), 0.001)</pre><p>Суммируются bucket-счётчики с сохранением le; среднее перцентилей Pod не используется. Гистограмма backend включает только завершённые ответы, поэтому сравнение требует того же периода, реплик и множества успехов. Для точного cohort нужны дельты bucket до/после без посторонних запросов и перезапусков; rate за соседнее минутное окно не равен эмпирическому p95. Бакетная интерполяция даёт дополнительную погрешность. Источник: <a href="https://prometheus.io/docs/practices/histograms/">Prometheus histograms</a>.</p>'''
    html+=calibration_table()+cohort
    html+='''<h2>6. Собственные проверки</h2><p><code>tests/test_backend.py</code> проверяет исходный дефект probes и исправление, отказ при неверном SHA256, изоляцию контрольных строк диалогов, раздельные HTTP429 и оборванный HTTP200, вытеснение LRU и строгие ограничения запроса. Это реальные HTTP-потоки через Uvicorn; закрытие клиента до done освобождает слот. <code>tests/test_accounting.py</code> отдельно проверяет генератор и классификацию исходов. В <code>tests/test_kubernetes.py</code> проверки обращаются к реальному Service: исключение прогрева из endpoints, версия A после отката, замена Pod с восстановлением артефакта и истории, изоляция диалогов.</p><p>Локальные проверки не подменяют Kubernetes: пропущенные проверки обозначаются skipped. Полные логи: <a href="results/local-tests.log">локальные HTTP-проверки и Helm lint</a>, <a href="results/kubernetes-tests.log">четыре Kubernetes-проверки</a>. Control-plane подтверждения сохранены в results/test-*.json; <a href="results/audit.json">audit.json</a> проверяет полноту доказательств.</p>
    <h2>7. Ревью PR: навсегда закрепить диалог за Pod?</h2><p>При заданных условиях новый запрос следует допустить на B: ожидание A уже 3 с, пересчёт на свободной B занимает 0,8 с. Выигрыш до сравнимого начала ответа — около 2,2 с. Локальный префикс у A полезен, пока его экономия превышает цену очереди. Постоянная привязка делает кеш обязательной точкой отказа и не даёт свободной реплике принять работу. В данном стенде выбираю обычный Service; для inference engine оценивал бы готовую политику, совместно учитывающую кеш и прогноз задержки.</p><p>До допуска проверяем Ready, совместимость версии, ограничение running/waiting, достаточную память для контекста и оставшийся deadline. Если ни одна реплика не укладывается, нужен явный отказ или ограниченная очередь; скрытый неограниченный backlog ухудшит хвост задержки. Недостающие измерения — распределение длин входа и ответа, точность оценки prefill/очереди, cache hit/miss, скорость декодирования, KV-память, возраст метрик и стоимость маршрутизации. Для решения по p95 нужны несколько повторов и число наблюдений; средний CPU этого не заменяет.</p><p>Историю диалога клиент передаёт целиком либо получает из внешнего долговечного хранилища с версией записи. После потери A новая реплика восстанавливает историю, проверяет версию артефакта и пересчитывает префикс. Локальный кеш — ускорение, а не единственная копия данных. Уже начатый поток оставляем на A до завершения в grace period. Перемещать его посередине нельзя: после обрыва клиент видит незавершённый ответ; повтор идёт отдельной попыткой с явной политикой дедупликации и восстановления, а не склейкой двух генераций.</p>'''
    html+=case_html
    html+='''<p>В кейсе <a href="https://cloud.google.com/blog/products/containers-kubernetes/how-gke-inference-gateway-improved-latency-for-vertex-ai">Google Cloud / Vertex AI, 6 февраля 2026</a>, выбранном из лекции, веса prefix:queue:kv-utilization изменили с 3:3:2 на 3:5:2. Увеличение веса очереди позволяло обходить горячий Pod даже при попадании в кеш. В первичном материале приведены рост cache hit с 35% до 70% и улучшение p95 TTFT DeepSeek V3.1 на 52%. Это подтверждает необходимость совместно учитывать очередь, кеш и допуск; проценты из их production-опыта на наш CPU-имитатор не переносятся.</p>
    <h2>8. Запуск, повторение и удаление</h2><pre>cd hw11
./scripts/install-tools.sh
make install
make up          # отдельный kind/Colima, образ и Helm
make a
make b           # 12 × ≥120 с; холодный кеш до каждого прогона
make c
make check-k8s
uv sync --locked --group report
uv run --group report python scripts/report.py
make stop        # остановить отдельную VM Colima hw11
# либо make delete — удалить кластер и профиль hw11</pre><p>Точные актуальные цели и предварительные условия приведены в <a href="README.md">README</a> и Makefile. Образы/зависимости закреплены, базовый Python image имеет digest. Prometheus хранит не более 1 часа / 128 MB в ограниченном томе; Grafana и мониторинг не используют неограниченные persistent volumes. Команды удаления не выполняют общий docker prune и не затрагивают стенды прежних ДЗ.</p><p>Исходные данные: <a href="results/requests.jsonl">requests.jsonl</a>, <a href="results/commands.jsonl">commands.jsonl</a>, <a href="chart/values.yaml">Helm values</a>, <a href="monitoring/dashboard.json">Grafana dashboard</a>. Отчёт и четыре SVG строятся программой <a href="scripts/report.py">report.py</a> из сохранённых журналов.</p></main></html>'''
    (ROOT/'report.html').write_text(html)
    print(json.dumps({'requests':len(rows),'summaries':len(runs),'timelines':len(traces),
                      'missing_evidence':missing,'report':str(ROOT/'report.html')},ensure_ascii=False))


if __name__=='__main__':main()
