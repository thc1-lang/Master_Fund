"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X } from "lucide-react";
import { useState } from "react";
import { Logo } from "./logo";
import { nav } from "@/lib/site";
export function Header() {
  const [open,setOpen]=useState(false);
  const path=usePathname();
  return <header className="site-header"><div className="container header-inner">
    <Logo />
    <nav className="desktop-nav" aria-label="Primary navigation">{nav.map(x=><Link className={path===x.href?"active":""} key={x.href} href={x.href}>{x.label}</Link>)}</nav>
    <Link className="header-contact" href="/contact">Enquire <span aria-hidden="true">↗</span></Link>
    <button type="button" className="menu-button" aria-label={open?"Close navigation":"Open navigation"} aria-expanded={open} aria-controls="mobile-navigation" onClick={()=>setOpen(!open)}>{open?<X size={23}/>:<Menu size={23}/>}</button>
  </div><nav id="mobile-navigation" className={`mobile-nav ${open?"mobile-open":""}`} aria-label="Mobile navigation">{nav.map(x=><Link key={x.href} href={x.href} onClick={()=>setOpen(false)}>{x.label}<span>↗</span></Link>)}</nav></header>;
}
