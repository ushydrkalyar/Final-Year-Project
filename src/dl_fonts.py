import sys
OUT=sys.argv[1]
import urllib.request, concurrent.futures as cf
fam="""Anton|Bebas Neue|Oswald|Luckiest Guy|Bangers|Righteous|Lobster|Pacifico|Fredoka|Baloo 2|Archivo Black|Archivo|Rubik|Poppins|Montserrat|Inter|Roboto|Roboto Condensed|Open Sans|Lato|Nunito|Raleway|Work Sans|DM Sans|Manrope|Outfit|Sora|Urbanist|Plus Jakarta Sans|League Spartan|Kanit|Barlow|Barlow Condensed|Teko|Saira Condensed|Exo 2|Orbitron|Michroma|Press Start 2P|Audiowide|Russo One|Black Ops One|Bungee|Staatliches|Passion One|Paytone One|Titan One|Chango|Concert One|Lilita One|Fugaz One|Alfa Slab One|Bowlby One|Knewave|Permanent Marker|Rock Salt|Caveat|Dancing Script|Satisfy|Great Vibes|Sacramento|Kaushan Script|Yellowtail|Playfair Display|Cinzel|Cormorant Garamond|Lora|Merriweather|DM Serif Display|Abril Fatface|Bodoni Moda|Libre Baskerville|Instrument Serif|Instrument Sans|Fraunces|Space Grotesk|Space Mono|JetBrains Mono|IBM Plex Sans|Josefin Sans|Quicksand|Comfortaa|Cabin|Karla|Mulish|Heebo|Figtree|Lexend|Syne|Unbounded|Big Shoulders Display|Anybody|Gloock|Zilla Slab|Special Elite|Creepster|Monoton|Shrikhand|Chewy|Gochi Hand|Days One|Squada One|Racing Sans One|Rammetto One|Sigmar One|Modak|Rubik Mono One|Dela Gothic One|Black Han Sans|Changa One|Holtwood One SC|Ultra|Oleo Script|Bakbak One|Lobster Two|Fjalla One|Pathway Gothic One|Alegreya Sans|Source Sans 3|Noto Sans|Ubuntu|Play|Rajdhani|Bree Serif|Marcellus|Yeseva One|Playfair Display SC|Amatic SC|Patrick Hand|Architects Daughter|Cookie|Bevan|Coiny|Gluten|Rowdies|Sniglet""".split("|")
cands=["{n}-Regular","{n}-Bold","{n}-ExtraBold","{n}-Black","{n}-Medium","{n}-SemiBold","{n}[wght]","{n}[wdth,wght]"]
def get(url):
    try:
        r=urllib.request.urlopen(url,timeout=25); return r.read()
    except Exception: return None
def job(f):
    n=f.replace(" ",""); d=n.lower(); got=[]
    for lic in("ofl","apache","ufl"):
        for c in cands:
            fn=c.format(n=n)+".ttf"
            url=f"https://raw.githubusercontent.com/google/fonts/main/{lic}/{d}/{fn.replace('[','%5B').replace(']','%5D').replace(',','%2C')}"
            b=get(url)
            if b and b[:4] in(b'\x00\x01\x00\x00',b'true',b'OTTO'):
                open(OUT+"/"+fn.replace('[','_').replace(']','').replace(',','-'),"wb").write(b); got.append(fn)
        if got: break
    return f,got
with cf.ThreadPoolExecutor(16) as ex: res=list(ex.map(job,fam))
ok=[r for r in res if r[1]]; bad=[r[0] for r in res if not r[1]]
print(len(ok),"families ok;",sum(len(r[1]) for r in ok),"files"); print("missing:",bad)
