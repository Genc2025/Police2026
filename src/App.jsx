import React,{useMemo,useState}from'react';

const professions=[
{icon:'♥',name:'Nursing',exam:'NCLEX-RN®',questions:'3,200+',tone:'purple'},
{icon:'⬡',name:'Law Enforcement',exam:'Sergeant Exam',questions:'2,500+',tone:'blue',active:true},
{icon:'✚',name:'EMS',exam:'EMT-B / Paramedic',questions:'1,800+',tone:'green'},
{icon:'◆',name:'Firefighting',exam:'Firefighter Exams',questions:'1,100+',tone:'orange'},
{icon:'▦',name:'Accounting',exam:'CPA Exam',questions:'2,900+',tone:'gold'},
{icon:'◆',name:'Education',exam:'Teacher Certification',questions:'1,600+',tone:'cyan'},
{icon:'♧',name:'Medical Assisting',exam:'CMA (AAMA)',questions:'1,200+',tone:'red'},
{icon:'Rx',name:'Pharmacy',exam:'Pharmacy Technician',questions:'1,100+',tone:'purple'},
{icon:'⚖',name:'Construction',exam:'OSHA & Safety',questions:'1,300+',tone:'blue'},
{icon:'✈',name:'Aviation',exam:'FAA Exams',questions:'1,000+',tone:'green'},
{icon:'◆',name:'Legal',exam:'Paralegal / Legal Asst.',questions:'1,400+',tone:'orange'},
{icon:'▣',name:'Information Tech',exam:'CompTIA A+',questions:'1,700+',tone:'purple'}];

export default function App(){
 const[q,setQ]=useState('');
 const visible=useMemo(()=>professions.filter(x=>(x.name+' '+x.exam).toLowerCase().includes(q.toLowerCase())),[q]);
 const openProfession=p=>{if(p.active)window.location.href='./police.html';};
 return <main className="portal">
  <nav className="nav"><div className="brand"><span className="brandMark">◇</span><div><b>ExamElite</b><small>Prepare. Practice. Succeed.</small></div></div><div className="navLinks"><a>Home</a><a>My Progress</a><a>Question Bank</a><a>Study Plan</a><a>Analytics</a><a>Community</a><a>Pricing</a></div><div className="navActions"><button>◐</button><button className="login">Log in</button><button className="signup">Sign up</button></div></nav>
  <div className="shell"><section className="landingHero"><div className="heroCopy"><h1>Choose Your Path.<br/><em>Master</em> Your Future.</h1><p>Premium question banks and smart tools for top professionals. One platform. Unlimited potential.</p><div className="benefits"><span><i>✓</i><b>High-Yield Questions<small>Exam-focused & updated</small></b></span><span><i>↗</i><b>Smart Analytics<small>Track. Improve. Succeed.</small></b></span><span><i>◎</i><b>Trusted Platform<small>Built for serious learners</small></b></span></div></div>
  <div className="compass"><div className="orbit"><div className="needle">◆</div></div><label className="float law">⬡ <b>Law Enforcement</b><small>Sergeant Exam</small></label><label className="float nurse">♥ <b>Nursing</b><small>NCLEX-RN®</small></label><label className="float ems">✚ <b>EMS</b><small>EMT Exam</small></label><label className="float fire">◆ <b>Firefighting</b><small>FF1 & Promotion</small></label><label className="float edu">◆ <b>Education</b><small>Teacher Exams</small></label></div></section>
  <section className="searchbar"><span>⌕</span><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search for a profession or exam..."/><button>All Categories⌄</button><button className="explore">▦ &nbsp; Explore All</button></section>
  <div className="sectionTitle"><h2>◆ &nbsp; Explore Professions</h2><span>Select your profession and start preparing with confidence.</span></div>
  <section className="professionGrid">{visible.map(p=><button key={p.name} className={'profession '+p.tone+(p.active?' active':'')} onClick={()=>openProfession(p)}><i>{p.icon}</i><strong>{p.name}</strong><span>{p.exam}</span><footer><small>{p.questions} Questions</small><b>›</b></footer></button>)}</section>
  <section className="platformStats"><span><i>♟</i><b>50,000+<small>Active Learners</small></b></span><span><i>▤</i><b>20,000+<small>High-Yield Questions</small></b></span><span><i>★</i><b>4.9/5<small>User Rating</small></b></span><span><i>⬡</i><b>99.9%<small>Uptime & Reliability</small></b></span><span><i>◎</i><b>Worldwide<small>Trusted by Learners</small></b></span></section>
 </div></main>;
}