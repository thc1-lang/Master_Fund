import Link from "next/link";
import { Logo } from "./logo";
import { nav } from "@/lib/site";
export function Footer(){return <footer className="site-footer"><div className="container">
<div className="footer-main"><div><Logo light/><p>Independent research across global markets.<br/>Built on analytical rigour and disciplined judgement.</p></div>
<div className="footer-links"><span>EXPLORE</span>{nav.map(x=><Link key={x.href} href={x.href}>{x.label}</Link>)}</div>
<div className="footer-links"><span>INFORMATION</span><Link href="/legal">Legal & disclosures</Link><Link href="/legal#privacy">Privacy</Link><Link href="/legal#cookies">Cookies</Link></div></div>
<div className="footer-bottom"><span>© {new Date().getFullYear()} Allen & Cooper. All rights reserved.</span><span>Independent multi-asset investment research.</span></div>
</div></footer>}
