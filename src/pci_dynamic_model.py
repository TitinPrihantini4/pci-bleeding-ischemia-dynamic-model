from pathlib import Path
import json, math
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.integrate import solve_ivp
from scipy.stats import qmc, rankdata, pearsonr
from scipy.optimize import least_squares

OUT=Path('/mnt/data/apcvp2026_pci_deescalation'); FIG=OUT/'figures'; FIG.mkdir(parents=True,exist_ok=True)
SEED=20260829
rng=np.random.default_rng(SEED)

# Literature anchors: observed/consensus quantities only. Structural coefficients remain provisional.
ANCHORS={
    'arc_hbr_hb_major':11.0,
    'arc_hbr_egfr_major':30.0,
    'arc_hbr_egfr_minor_low':30.0,
    'arc_hbr_egfr_minor_high':59.0,
    'arc_hbr_major_bleeding_1y':0.04,
    'talos_bleed_control':0.056,
    'talos_bleed_deesc':0.030,
    'talos_ischemic_control':0.031,
    'talos_ischemic_deesc':0.021,
    'twilight_bleed_dapt':0.071,
    'twilight_bleed_mono':0.040,
    'master_bleed_standard':0.094,
    'master_bleed_abbrev':0.065,
    'smartchoice_bleed_dapt':0.034,
    'smartchoice_bleed_mono':0.020,
    'esc_no_deesc_before_days':30,
}

# Provisional structural parameters chosen to generate event-rate ranges compatible with literature anchors.
BASE=dict(
    hb_mid=11.5, hb_scale=0.85, egfr_mid=50.0, egfr_scale=14.0,
    w_hb_B=0.85, w_g_B=0.70, w_pb_B=0.80,
    w_hb_I=0.20, w_g_I=0.65, w_pb_I=0.10,
    b0=0.026, i0=0.090, treatment_bleed=0.70, treatment_ischemia=1.15,
    ischemic_decay=0.180,
    kB=1.05, rB=0.28, kI=1.00, rI=0.32,
    eta_B=0.16, eta_I=0.22,
    lambda_bleed=1.0, lambda_isch=1.70,
    switch_cost=0.0030,
    margin_threshold=0.0,
)

RANGES={
    'w_hb_B':(0.60,1.10),'w_g_B':(0.45,0.95),'w_pb_B':(0.55,1.10),
    'w_hb_I':(0.05,0.40),'w_g_I':(0.40,0.90),'w_pb_I':(0.00,0.30),
    'b0':(0.020,0.036),'i0':(0.060,0.120),
    'treatment_bleed':(0.50,0.90),'treatment_ischemia':(0.85,1.40),
    'ischemic_decay':(0.120,0.240),'kB':(0.80,1.30),'rB':(0.18,0.40),
    'kI':(0.80,1.25),'rI':(0.20,0.44),'lambda_bleed':(0.8,1.3),'lambda_isch':(1.30,2.10),
}

def logistic(x): return 1/(1+np.exp(-x))

def modifiers(hb,egfr,prior_bleed,p=BASE):
    qhb=logistic((p['hb_mid']-hb)/p['hb_scale'])
    qg=logistic((p['egfr_mid']-egfr)/p['egfr_scale'])
    pb=float(prior_bleed)
    return qhb,qg,pb

def hazards(month,hb,egfr,prior_bleed,u,p=BASE):
    # month is months after the 30-day landmark.
    qhb,qg,pb=modifiers(hb,egfr,prior_bleed,p)
    hB=p['b0']*np.exp(p['w_hb_B']*qhb+p['w_g_B']*qg+p['w_pb_B']*pb+p['treatment_bleed']*u)
    hI=p['i0']*np.exp(p['w_hb_I']*qhb+p['w_g_I']*qg+p['w_pb_I']*pb-p['treatment_ischemia']*u-p['ischemic_decay']*month)
    return hB,hI

def rhs(month,y,hb,egfr,prior_bleed,u,p=BASE):
    B,I=y
    hB,hI=hazards(month,hb,egfr,prior_bleed,u,p)
    dB=p['kB']*hB*(1-B)-p['rB']*B
    dI=p['kI']*hI*(1-I)-p['rI']*I
    return np.array([dB,dI])

def margin(month,B,I,hb,egfr,prior_bleed,u,p=BASE):
    hB,hI=hazards(month,hb,egfr,prior_bleed,u,p)
    # positive: bleeding burden dominates and de-escalation becomes theoretically favorable
    return p['lambda_bleed']*(B+hB)-p['lambda_isch']*(I+hI)

def regime(psi):
    if psi < -0.035: return 'Ischemic-dominant'
    if psi < 0.0: return 'Balanced-low margin'
    if psi < 0.050: return 'Bleeding-dominant'
    return 'De-escalation-favorable'

def simulate(hb=13.5,egfr=80,prior_bleed=0,u=1.0,p=BASE,horizon=11.0,dt=0.02,y0=(0.025,0.055)):
    tt=np.arange(0,horizon+1e-9,dt)
    sol=solve_ivp(lambda t,y: rhs(t,y,hb,egfr,prior_bleed,u,p),(0,horizon),y0,t_eval=tt,rtol=1e-9,atol=1e-11,method='RK45')
    B,I=sol.y
    psi=np.array([margin(t,b,i,hb,egfr,prior_bleed,u,p) for t,b,i in zip(tt,B,I)])
    return tt,B,I,psi

def _annual_hazard_components(hb,egfr,pb,u,p=BASE):
    # hB is constant over model time; hI(t)=cI*exp(-decay*t)
    qhb,qg,pbv=modifiers(hb,egfr,pb,p)
    cB=p['b0']*np.exp(p['w_hb_B']*qhb+p['w_g_B']*qg+p['w_pb_B']*pbv+p['treatment_bleed']*u)
    cI=p['i0']*np.exp(p['w_hb_I']*qhb+p['w_g_I']*qg+p['w_pb_I']*pbv-p['treatment_ischemia']*u)
    return cB,cI

def cumulative_event_prob(hb,egfr,pb,u,p=BASE,horizon=11.0,dt=None):
    cB,cI=_annual_hazard_components(hb,egfr,pb,u,p)
    HB=cB*horizon/12.0
    d=p['ischemic_decay']
    HI=cI*((1-np.exp(-d*horizon))/d)/12.0 if d>1e-12 else cI*horizon/12.0
    return 1-np.exp(-HB),1-np.exp(-HI)

def expected_loss(hb,egfr,pb,switch_month,p=BASE,horizon=11.0,dt=None):
    s=float(np.clip(switch_month,0,horizon)); d=p['ischemic_decay']
    b1,i1=_annual_hazard_components(hb,egfr,pb,1.0,p)
    b2,i2=_annual_hazard_components(hb,egfr,pb,0.55,p)
    HB=(b1*s+b2*(horizon-s))/12.0
    if d>1e-12:
        HI=(i1*(1-np.exp(-d*s))/d + i2*(np.exp(-d*s)-np.exp(-d*horizon))/d)/12.0
    else:
        HI=(i1*s+i2*(horizon-s))/12.0
    pB=1-np.exp(-HB); pI=1-np.exp(-HI)
    return p['lambda_bleed']*pB+p['lambda_isch']*pI+(p['switch_cost'] if s<horizon else 0),pB,pI

def optimal_switch(hb,egfr,pb,p=BASE,horizon=11.0):
    grid=np.linspace(0,horizon,56)
    vals=[expected_loss(hb,egfr,pb,s,p,horizon) for s in grid]
    j=np.argmin([v[0] for v in vals])
    return grid[j],vals[j][0],vals[j][1],vals[j][2]

# Reference profiles and risk trajectories
profiles={
 'Low vulnerability':(13.8,85,0),
 'Renal vulnerability':(13.0,35,0),
 'Anemia':(10.8,75,0),
 'Prior bleeding':(13.0,75,1),
 'Combined HBR':(10.5,25,1),
}
rows=[]
for name,(hb,g,pb) in profiles.items():
    sw,loss,pB,pI=optimal_switch(hb,g,pb)
    pBpot,pIpot=cumulative_event_prob(hb,g,pb,1.0)
    pBdec,pIdec=cumulative_event_prob(hb,g,pb,.55)
    rows.append(dict(profile=name,Hb_g_dL=hb,eGFR=g,prior_bleeding=pb,optimal_switch_month_after_30d=sw,
                     optimal_calendar_day_post_index=30+30.44*sw,loss=loss,
                     bleed_prob_potent=pBpot,ischemic_prob_potent=pIpot,bleed_prob_deesc=pBdec,ischemic_prob_deesc=pIdec,
                     bleeding_absolute_change=pBdec-pBpot,ischemic_absolute_change=pIdec-pIpot))
sc=pd.DataFrame(rows);sc.to_csv(OUT/'Scenario_Results.csv',index=False)

# Reference latent dynamics for moderate vulnerability
ref_hb,ref_g,ref_pb=11.5,45,0
tr=simulate(ref_hb,ref_g,ref_pb,1.0);t,B,I,psi=tr
ref=pd.DataFrame({'month_after_30d':t,'bleeding_burden':B,'ischemic_burden':I,'net_margin':psi,'regime':[regime(x) for x in psi]})
ref.to_csv(OUT/'Reference_Trajectory.csv',index=False)

# Risk surface / optimal switch grid
surf=[]
for hb in np.arange(9.5,14.6,0.25):
  for egfr in np.arange(20,101,5):
    for pb in [0,1]:
      sw,loss,pB,pI=optimal_switch(hb,egfr,pb)
      surf.append((hb,egfr,pb,sw,loss,pB,pI))
surf=pd.DataFrame(surf,columns=['hemoglobin_g_dL','eGFR_mL_min_1_73m2','prior_bleeding','optimal_switch_month_after_30d','expected_loss','bleed_probability','ischemic_probability'])
surf.to_csv(OUT/'Bleeding_Ischemia_Risk_Surface.csv',index=False)

# LHS global sensitivity at reference profile, outcome optimal switch and loss
keys=list(RANGES.keys()); n=320
sampler=qmc.LatinHypercube(d=len(keys),seed=SEED);U=sampler.random(n)
samples=[];outs=[]
for r in U:
    p=BASE.copy()
    vals=[]
    for j,k in enumerate(keys):
        lo,hi=RANGES[k];v=lo+(hi-lo)*r[j];p[k]=v;vals.append(v)
    sw,loss,pB,pI=optimal_switch(ref_hb,ref_g,ref_pb,p)
    samples.append(vals);outs.append((sw,loss,pB,pI))
S=pd.DataFrame(samples,columns=keys);O=pd.DataFrame(outs,columns=['optimal_switch_month','expected_loss','bleed_probability','ischemic_probability'])
S.to_csv(OUT/'LHS_Parameter_Samples.csv',index=False);O.to_csv(OUT/'LHS_Model_Outputs.csv',index=False)
# PRCC via rank residuals
def residualize(y,X):
    X=np.column_stack([np.ones(len(X)),X]); return y-X@np.linalg.lstsq(X,y,rcond=None)[0]
R=np.column_stack([rankdata(S[c]) for c in keys]); y=rankdata(O['optimal_switch_month'])
pr=[]
for j,k in enumerate(keys):
    others=np.delete(R,j,axis=1)
    rx=residualize(R[:,j],others); ry=residualize(y,others)
    rr,pv=pearsonr(rx,ry);pr.append((k,rr,pv))
pr=pd.DataFrame(pr,columns=['parameter','PRCC_optimal_switch_month','p_value']).sort_values('PRCC_optimal_switch_month',key=lambda s:abs(s),ascending=False)
pr.to_csv(OUT/'Global_Sensitivity_PRCC.csv',index=False)

# Local elasticity around reference, +1%
base_sw,base_loss,_,_=optimal_switch(ref_hb,ref_g,ref_pb)
el=[]
for k in keys:
    p=BASE.copy();p[k]*=1.01
    sw,loss,_,_=optimal_switch(ref_hb,ref_g,ref_pb,p)
    e_sw=((sw-base_sw)/(base_sw+1e-9))/0.01 if base_sw>0 else np.nan
    e_loss=((loss-base_loss)/base_loss)/0.01
    el.append((k,e_sw,e_loss))
pd.DataFrame(el,columns=['parameter','elasticity_switch_time','elasticity_expected_loss']).to_csv(OUT/'Local_Elasticity.csv',index=False)

# Stochastic perturbation of Hb and eGFR around combined moderate profile, yielding switch timing distribution
st=[]
for i in range(600):
    hb=max(8.5,rng.normal(ref_hb,0.65)); g=max(15,rng.normal(ref_g,10)); pb=int(rng.random()<0.18)
    sw,loss,pB,pI=optimal_switch(hb,g,pb)
    st.append((hb,g,pb,sw,loss,pB,pI))
st=pd.DataFrame(st,columns=['hemoglobin','eGFR','prior_bleeding','optimal_switch_month','expected_loss','bleed_probability','ischemic_probability'])
st.to_csv(OUT/'Stochastic_Perturbation_Results.csv',index=False)

# Numerical robustness - RK45 vs DOP853 latent reference trajectory
rob=[]
for method in ['RK45','DOP853']:
  for maxstep in [1.0,.5,.25,.1]:
    tt=np.linspace(0,11,1101)
    sol=solve_ivp(lambda x,y:rhs(x,y,ref_hb,ref_g,ref_pb,1.0,BASE),(0,11),(0.025,0.055),t_eval=tt,method=method,max_step=maxstep,rtol=1e-9,atol=1e-11)
    ps=np.array([margin(x,b,i,ref_hb,ref_g,ref_pb,1.0,BASE) for x,b,i in zip(sol.t,sol.y[0],sol.y[1])])
    rob.append((method,maxstep,sol.y[0,-1],sol.y[1,-1],ps[-1]))
pd.DataFrame(rob,columns=['solver','max_step_month','terminal_B','terminal_I','terminal_margin']).to_csv(OUT/'Numerical_Robustness.csv',index=False)

# Synthetic AIC recovery: generate pseudo observations from dynamic hazards at monthly times
months=np.arange(0,12)
trueB=np.array([hazards(m,ref_hb,ref_g,0,1.0)[0] for m in months])
trueI=np.array([hazards(m,ref_hb,ref_g,0,1.0)[1] for m in months])
obsB=trueB+rng.normal(0,0.0025,len(months));obsI=trueI+rng.normal(0,0.0025,len(months))
obs=np.r_[obsB,obsI]

def aic(rss,n,k): return n*np.log(rss/n)+2*k
# Model 1 full: fit scaling and decay around true structural forms
# parameter [bscale, iscale, decay, trI]
def pred_full(par):
    bs,is_,dec,tr=par
    p=BASE.copy();p['b0']=bs;p['i0']=is_;p['ischemic_decay']=dec;p['treatment_ischemia']=tr
    return np.r_[[hazards(m,ref_hb,ref_g,0,1.0,p)[0] for m in months],[hazards(m,ref_hb,ref_g,0,1.0,p)[1] for m in months]]
fit=least_squares(lambda z:pred_full(z)-obs,[BASE['b0'],BASE['i0'],BASE['ischemic_decay'],BASE['treatment_ischemia']],bounds=([.005,.005,.01,.1],[.1,.15,.4,1.5]))
mods=[]
for name,pred,k in []: pass
rss=np.sum((fit.fun)**2);mods.append(('Full competing-hazard structural model',aic(rss,len(obs),4),rss,4))
# linear time model each outcome intercept/slope (4 params)
X=np.c_[np.ones(len(months)),months]
bb=np.linalg.lstsq(X,obsB,rcond=None)[0];ii=np.linalg.lstsq(X,obsI,rcond=None)[0];pred=np.r_[X@bb,X@ii];rss=np.sum((pred-obs)**2);mods.append(('Independent linear hazard model',aic(rss,len(obs),4),rss,4))
# constant hazards 2 params
pred=np.r_[np.repeat(obsB.mean(),12),np.repeat(obsI.mean(),12)];rss=np.sum((pred-obs)**2);mods.append(('Constant competing-hazard model',aic(rss,len(obs),2),rss,2))
# shared exponential decay 3 params Bconst, I0, decay

def pred_exp(z):
    b,i,d=z;return np.r_[np.repeat(b,12),i*np.exp(-d*months)]
ft=least_squares(lambda z:pred_exp(z)-obs,[obsB.mean(),obsI[0],.1],bounds=([0,0,0],[.2,.2,1]))
rss=np.sum(ft.fun**2);mods.append(('Shared exponential ischemic-decay model',aic(rss,len(obs),3),rss,3))
mc=pd.DataFrame(mods,columns=['model','AIC_synthetic','RSS','fitted_parameters']).sort_values('AIC_synthetic');mc['delta_AIC']=mc['AIC_synthetic']-mc['AIC_synthetic'].min();mc.to_csv(OUT/'Synthetic_Model_Comparison_AIC.csv',index=False)

# Figures
plt.figure(figsize=(9,5.5));
for name,(hb,g,pb) in profiles.items():
    vals=[expected_loss(hb,g,pb,s)[0] for s in np.linspace(0,11,111)]
    plt.plot(np.linspace(0,11,111),vals,label=name)
plt.xlabel('Switch month after 30-day landmark');plt.ylabel('Illustrative expected clinical loss');plt.title('Sequential De-escalation Loss Curves');plt.legend(fontsize=8);plt.tight_layout();plt.savefig(FIG/'Figure_1_Sequential_Loss_Curves.png',dpi=220);plt.close()

# heatmap risk surface for no prior bleeding
for pb in [0,1]:
    z=surf[surf.prior_bleeding==pb].pivot(index='eGFR_mL_min_1_73m2',columns='hemoglobin_g_dL',values='optimal_switch_month_after_30d')
    plt.figure(figsize=(9,5.7));im=plt.imshow(z.values,origin='lower',aspect='auto',extent=[z.columns.min(),z.columns.max(),z.index.min(),z.index.max()]);plt.colorbar(im,label='Optimal switch month after day 30');plt.xlabel('Hemoglobin (g/dL)');plt.ylabel('eGFR (mL/min/1.73 m²)');plt.title('Dynamic Antithrombotic Switching Surface' + (' With Prior Bleeding' if pb else ' Without Prior Bleeding'));plt.tight_layout();plt.savefig(FIG/f'Figure_{2+pb}_Switching_Surface_PB{pb}.png',dpi=220);plt.close()

plt.figure(figsize=(9,5.5));plt.plot(t,B,label='Latent bleeding burden');plt.plot(t,I,label='Latent ischemic burden');plt.plot(t,psi,label='Net bleeding–ischemia margin');plt.axhline(0,ls='--');plt.xlabel('Months after 30-day landmark');plt.ylabel('Dimensionless state');plt.title('Reference Competing-Risk Dynamics');plt.legend();plt.tight_layout();plt.savefig(FIG/'Figure_4_Reference_Dynamics.png',dpi=220);plt.close()

pplot=pr.head(12).sort_values('PRCC_optimal_switch_month');plt.figure(figsize=(8,6));plt.barh(pplot.parameter,pplot.PRCC_optimal_switch_month);plt.xlabel('PRCC with optimal switch month');plt.title('Global Sensitivity of the Switching Boundary');plt.tight_layout();plt.savefig(FIG/'Figure_5_Global_Sensitivity_PRCC.png',dpi=220);plt.close()

plt.figure(figsize=(8.5,5.5));plt.hist(st.optimal_switch_month,bins=np.arange(-.05,11.2,.5),edgecolor='black');plt.xlabel('Optimal switch month after day 30');plt.ylabel('Stochastic scenarios');plt.title('Uncertainty Distribution of Theoretical Switching Time');plt.tight_layout();plt.savefig(FIG/'Figure_6_Stochastic_Switching_Time.png',dpi=220);plt.close()

# scenario comparative bars
m=sc.set_index('profile');x=np.arange(len(m));w=.35
plt.figure(figsize=(9,5.5));plt.bar(x-w/2,100*m.bleed_prob_potent,w,label='Potent DAPT');plt.bar(x+w/2,100*m.bleed_prob_deesc,w,label='De-escalated');plt.xticks(x,m.index,rotation=20,ha='right');plt.ylabel('Model-implied bleeding probability (%)');plt.title('Scenario Stress Test of Bleeding Risk');plt.legend();plt.tight_layout();plt.savefig(FIG/'Figure_7_Scenario_Bleeding.png',dpi=220);plt.close()

plt.figure(figsize=(8.5,5.2));mm=mc.sort_values('delta_AIC',ascending=False);plt.barh(mm.model,mm.delta_AIC);plt.xlabel('ΔAIC from best model');plt.title('Synthetic Structural-Recovery Benchmark');plt.tight_layout();plt.savefig(FIG/'Figure_8_Synthetic_AIC.png',dpi=220);plt.close()

# summary
summary={
 'seed':SEED,
 'reference_profile':{'hemoglobin':ref_hb,'eGFR':ref_g,'prior_bleeding':ref_pb,'optimal_switch':optimal_switch(ref_hb,ref_g,ref_pb)},
 'scenarios':sc.to_dict(orient='records'),
 'stochastic':{'median_switch_month':float(st.optimal_switch_month.median()),'p2_5':float(st.optimal_switch_month.quantile(.025)),'p97_5':float(st.optimal_switch_month.quantile(.975)),'immediate_fraction':float((st.optimal_switch_month==0).mean()),'late_no_switch_fraction':float((st.optimal_switch_month>=11).mean())},
 'top_prcc':pr.head(8).to_dict(orient='records'),
 'aic':mc.to_dict(orient='records'),
}
(OUT/'Analysis_Summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
