from PIL import Image, ImageDraw
def draw(n):
    S=n*4; im=Image.new("RGBA",(S,S),(0,0,0,0)); d=ImageDraw.Draw(im)
    r=S*0.18; d.rectangle([0,0,S-1,S-1],fill=(15,42,68,255)); d.rectangle([0,S*0.86,S-1,S-1],fill=(15,123,138,255))
    w=(255,255,255,255); lw=max(2,int(S*0.045))
    cx=S/2; top=S*0.16; beam=S*0.30
    d.line([cx,top,cx,S*0.74],fill=w,width=lw)                      # стойка
    d.ellipse([cx-S*0.04,top-S*0.04,cx+S*0.04,top+S*0.04],fill=w)
    d.line([S*0.2,beam,S*0.8,beam],fill=w,width=lw)                 # коромысло
    for x in (S*0.2,S*0.8):
        d.line([x,beam,x-S*0.11,S*0.52],fill=w,width=max(1,lw//2)); d.line([x,beam,x+S*0.11,S*0.52],fill=w,width=max(1,lw//2))
        d.chord([x-S*0.14,S*0.42,x+S*0.14,S*0.62],0,180,fill=w)      # чаши
    d.rounded_rectangle([cx-S*0.17,S*0.72,cx+S*0.17,S*0.78],radius=S*0.02,fill=w)  # основание
    m=Image.new("L",(S,S),0); ImageDraw.Draw(m).rounded_rectangle([0,0,S-1,S-1],radius=r,fill=255); im.putalpha(m)
    return im.resize((n,n),Image.LANCZOS)
sizes=[16,20,24,32,40,48,64,128,256]
imgs=[draw(s) for s in sizes]
imgs[-1].save("app.ico",sizes=[(s,s) for s in sizes],append_images=imgs[:-1])
draw(512).save("app_512.png"); draw(64).save("app_64.png")
# баннер мастера установки NSIS (164x314) и шапка (150x57), BMP 24-bit
side=Image.new("RGB",(164,314),(15,42,68)); ic=draw(96).convert("RGBA"); side.paste(ic,(34,40),ic)
dd=ImageDraw.Draw(side); dd.rectangle([0,300,164,314],fill=(15,123,138)); side.save("wizard.bmp")
hdr=Image.new("RGB",(150,57),(255,255,255)); ic2=draw(48).convert("RGBA"); hdr.paste(ic2,(96,4),ic2); hdr.save("header.bmp")
print("ok")
