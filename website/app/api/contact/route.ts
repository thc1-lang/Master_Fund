import { NextResponse } from "next/server";
export const runtime="nodejs";
const subjects=new Set(["Research enquiry","Institutional partnership","General enquiry"]);
function isString(x:unknown,max:number){return typeof x==="string"&&x.length<=max;}
function clean(s:string){return s.replace(/[<>\u0000-\u001f]/g, " ").trim();}
export async function POST(request:Request){
  let b:Record<string,unknown>;
  try{b=await request.json();if(!b||typeof b!=="object"||Array.isArray(b))throw new Error();}catch{return NextResponse.json({error:"Invalid form submission."},{status:400});}
  if(typeof b.website==="string"&&b.website.trim())return NextResponse.json({ok:true});
  if(!isString(b.name,100)||!isString(b.email,200)||!isString(b.organisation||"",150)||!isString(b.message,5000)||!isString(b.subject,100))return NextResponse.json({error:"Invalid or oversized field."},{status:400});
  const name=clean(b.name as string),email=clean(b.email as string),organisation=clean((b.organisation||"") as string),message=clean(b.message as string),subject=clean(b.subject as string);
  if(name.length<2||message.length<15||!subjects.has(subject)||!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))return NextResponse.json({error:"Please check all required fields."},{status:400});
  const apiKey=process.env.RESEND_API_KEY,to=process.env.CONTACT_TO_EMAIL,from=process.env.CONTACT_FROM_EMAIL;
  if(!apiKey||!to||!from){return NextResponse.json({error:"Enquiries are not configured yet. Please try again after launch."},{status:503});}
  try{
    const result=await fetch("https://api.resend.com/emails",{method:"POST",headers:{Authorization:`Bearer ${apiKey}`,"Content-Type":"application/json"},body:JSON.stringify({from,to:[to],reply_to:email,subject:`Allen & Cooper — ${subject}`,text:`Name: ${name}\nEmail: ${email}\nOrganisation: ${organisation||"Not provided"}\nSubject: ${subject}\n\n${message}`}),signal:AbortSignal.timeout(10000)});
    if(!result.ok){console.error("Contact email provider failure:",result.status);return NextResponse.json({error:"Enquiry delivery is temporarily unavailable."},{status:502});}
    return NextResponse.json({ok:true});
  }catch(e){console.error("Contact delivery exception",e instanceof Error?e.message:"unknown");return NextResponse.json({error:"Enquiry delivery is temporarily unavailable."},{status:502});}
}
