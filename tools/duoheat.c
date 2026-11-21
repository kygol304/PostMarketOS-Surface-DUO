// duoheat: Surface Duo touch in heatmap mode -> two multitouch uinput touchscreens (one per panel). Own code, 01.11.2026.
// After SET_FEATURE {0x05, 0x01} (like Microsoft's touchpen service, proven from its binary) the controller
// (spi-hid 045E:0C01, /dev/hidraw0) delivers raw frames per panel: report 0x3F = left panel (DSI-1), 0x0D = right (DSI-2),
// 1500 bytes, from byte 26 a 27 x 21 grid (1 byte per crossing point, rest 180, a finger lowers it by ~100, noise ~0.6),
// ~100 frames/s while something rests on it, otherwise silence. Here: find blobs (local maxima), centroid, reject palms,
// track fingers, report as MT protocol B with 10 slots. Device names/IDs as in duotouch (mapping to DSI-1/2 stays).
// Exit (SIGTERM): lift all fingers, put the controller back into fallback mode via {0x05, 0x00} (duotouch can take over).
// Findings: ~/Hardware/Surface-Duo-1/TOUCH-GRABUNG-HEATMAP.md
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <time.h>
#include <signal.h>
#include <math.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/hidraw.h>
#include <linux/uinput.h>
#include <linux/input.h>

#define GW 27                 /* grid: columns (runs along the long panel side) */
#define GH 21                 /* grid: rows */
#define HEAT_OFF 26           /* start of the heatmap in the report */
#define REST 180              /* rest value per point (the firmware calibrates itself) */
#define T_ON 35               /* new finger from this signal at the maximum */
#define T_KEEP 20             /* an existing finger lives on down to here (hysteresis) */
#define T_FLOOR 8             /* weight floor for the centroid */
#define PALM_CELLS 16         /* blob with more points >= T_KEEP in the 5x5 neighbourhood = palm, reject */
#define HAND_CELLS 45         /* this many points >= T_KEEP on one panel = a hand rests on it (back side): lift everything */
#define MERGE_CELLS 1.6       /* two maxima closer than this = the same finger */
#define TRACK_PX 260.0        /* frame-to-frame matching: max. jump in device pixels (~100 Hz) */
#define SLOP_PX 24.0          /* dead zone like duotouch/Android «touch slop»: GTK treats ~7 logical px of wobble as a drag */
static int QUIET_MS = 60;    /* no frame for this long = everything lifted (the controller is silent without touch);
                                 DUO_QUIET_MS in /etc/duo/touch.conf (lower = taps register sooner, keep >= ~35 at 100 Hz) */
#define SLOTS 10
/* Ghosts (01.11. 13:34: a 9 ms touch on the terminal's close button while typing closed the window): a new
 * blob is only reported once it is there again in the next frame (~10 ms). Real taps last > 30 ms. */
#define CONFIRM_FRAMES 2
#define PW 1350
#define PH 1800
/* Calibration 01.11.2026 (heatkal.py + heatfit.py, target points shown by the shell): grid (gx, gy) -> device pixels
 * x = c0*gx + c1*gy + c2, y = c3*gx + c4*gy + c5. Mean residual error 8 px (duotouch: 11-17 px). */
#define CAL_L { -0.7828, -64.5060, 1339.6392, 67.6757, -0.3957, 22.8883 }   /* 0x3F, DSI-1, 6 points, residual 7.6 px */
#define CAL_R { -1.0110, -66.4916, 1344.9532, 68.5255,  0.8634, -13.7480 }  /* 0x0D, DSI-2, 11 points, residual 8.6 px */

struct contact { int active, id; double x, y, sx, sy, dx, dy; int slopped, seen, frames, peak, area; double gx, gy; long long t0; int top; double yoff; int snap; double fx, fy; int homechk, homeblock; };
/* DuoPort : le capteur n'a qu'une rangée tout en haut : la barre de Phosh (0-64 px) et l'en-tête des applis juste
 * dessous donnent la même mesure (≈51 px). Un doigt posé en haut qui GLISSE vers le bas va à la barre (notifications) ;
 * un doigt posé en haut qui reste IMMOBILE (appui, appui long) va à l'appli, juste sous la barre (TOP_APP_Y). */
#define TOP_BAND 90
/* UI scale of the outputs (/etc/duo/scale, default 2): Phosh's bars are 32 (top) and 15 (home) logical px high */
static double ui_scale = 2.0;
#define TOP_APP_Y (52.5*ui_scale)   /* just under Phosh's top bar (105 px at scale 2) */
#define TOP_DRAG_PX 50.0   /* DuoPort 12/11 : 30 -> 50, un appui qui tremble ne déroule plus la barre */
#define TOP_HOLD_FRAMES 12   /* active: 0 free, 1 candidate, 2 reported */
struct panel { int rid, fd, pid; const char *name, *mute; double cal[6]; struct contact c[SLOTS]; long long last_frame; int any; };
static volatile sig_atomic_t term;
static void on_term(int s){ (void)s; term=1; }
static long long now_ms(void){ struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts); return ts.tv_sec*1000LL+ts.tv_nsec/1000000; }
static void emit(int fd,int t,int c,int v){ struct input_event e={0}; e.type=t; e.code=c; e.value=v; if(write(fd,&e,sizeof e)<0){} }
static int next_id=1;

static int mk_uinput(struct panel *p){
  int fd=open("/dev/uinput",O_WRONLY|O_NONBLOCK); if(fd<0){perror("/dev/uinput");return -1;}
  ioctl(fd,UI_SET_EVBIT,EV_ABS); ioctl(fd,UI_SET_EVBIT,EV_KEY); ioctl(fd,UI_SET_EVBIT,EV_SYN);
  ioctl(fd,UI_SET_KEYBIT,BTN_TOUCH); ioctl(fd,UI_SET_PROPBIT,INPUT_PROP_DIRECT);
  int codes[]={ABS_X,ABS_Y,ABS_MT_POSITION_X,ABS_MT_POSITION_Y,ABS_MT_SLOT,ABS_MT_TRACKING_ID};
  for(unsigned i=0;i<sizeof codes/sizeof *codes;i++) ioctl(fd,UI_SET_ABSBIT,codes[i]);
  struct uinput_user_dev ud={0}; snprintf(ud.name,sizeof ud.name,"%s",p->name); ud.id.bustype=BUS_SPI; ud.id.vendor=0x045e; ud.id.product=p->pid; ud.id.version=2;
  ud.absmax[ABS_X]=ud.absmax[ABS_MT_POSITION_X]=PW-1; ud.absmax[ABS_Y]=ud.absmax[ABS_MT_POSITION_Y]=PH-1;
  ud.absmax[ABS_MT_SLOT]=SLOTS-1; ud.absmax[ABS_MT_TRACKING_ID]=65535;
  if(write(fd,&ud,sizeof ud)<0){perror("uinput dev");return -1;}
  if(ioctl(fd,UI_DEV_CREATE)){perror("UI_DEV_CREATE");return -1;}
  p->fd=fd; return 0;
}

/* lift the finger in slot i (without SYN) */
static int heatlog(void){ return !access("/run/duo/heatlog",F_OK); }
/* DuoPort : la bande « haut d'écran » (rejeu sous la barre de Phosh) ne vaut que pour la dalle qui porte la barre,
 * en orientation normale. duo-posture écrit la lettre de cette dalle dans /run/duo/barre (A, B, ou vide si tournée).
 * Lu au plus toutes les 500 ms. Sans fichier : A (comportement d'origine). */
/* DuoPort 12/11 : étirement horizontal des bords, propre à chaque dalle (mesuré : la dalle A ne descend pas
 * sous x≈80 au bord gauche, la dalle B atteint x≈45). Réglable : DUO_X_STRETCH_A / DUO_X_STRETCH_B. */
static double etirement_x(const struct panel *p, int droite){
  /* mesuré le 12/11 (x au posé) : A 68..1298, B 28..1304 -> A étiré des deux côtés, B seulement à droite */
  static double k[4]={0,0,0,0}; int b=p->name[18]=='B', i=b*2+droite;
  static const char *noms[4]={"DUO_X_STRETCH_A_G","DUO_X_STRETCH_A_D","DUO_X_STRETCH_B_G","DUO_X_STRETCH_B_D"};
  static const double defaut[4]={1.25,1.25,1.0,1.25};
  if(!k[i]){ const char *e=getenv(noms[i]); k[i]=e?atof(e):defaut[i]; if(k[i]<1) k[i]=1; }
  return k[i];
}
/* calibration offsets per panel (physical px), from /etc/duo/touch.conf or the environment:
 * DUO_X_OFFSET_A, DUO_Y_OFFSET_A, DUO_X_OFFSET_B, DUO_Y_OFFSET_B (default 0). Measure with duo-touch-test. */
static double decalage(const struct panel *p, int axe_y){
  static double v[4]; static int lu[4]; int i=(p->name[18]=='B')*2+axe_y;
  static const char *noms[4]={"DUO_X_OFFSET_A","DUO_Y_OFFSET_A","DUO_X_OFFSET_B","DUO_Y_OFFSET_B"};
  if(!lu[i]){ const char *e=getenv(noms[i]); v[i]=e?atof(e):0; lu[i]=1; }
  return v[i];
}
/* /etc/duo/touch.conf (lignes NOM=valeur, # commentaires) : réglages DUO_* lus au démarrage, prioritaires sur
 * l'environnement. duoheat tourne hors du chroot : on cherche d'abord /r/etc/duo/touch.conf. */
static void charge_reglages(void){
  const char *chemins[]={"/r/etc/duo/touch.conf","/etc/duo/touch.conf"};
  for(int k=0;k<2;k++){
    FILE *f=fopen(chemins[k],"r"); if(!f) continue;
    char l[256];
    while(fgets(l,sizeof l,f)){
      char *e=strchr(l,'='); if(l[0]=='#'||!e||strncmp(l,"DUO_",4)) continue;
      *e=0; char *v=e+1; v[strcspn(v," \t\r\n#")]=0; setenv(l,v,1);
      fprintf(stderr,"duoheat: %s=%s (%s)\n",l,v,chemins[k]);
    }
    fclose(f); return;
  }
}
static int barre_sur(const struct panel *p){
  static long long lu=0; static char b='A';
  long long t=now_ms();
  if(t-lu>500){ lu=t; FILE *f=fopen("/run/duo/barre","r"); if(f){ int ch=fgetc(f); b=(ch=='A'||ch=='B')?(char)ch:'-'; fclose(f); } else b='A'; }
  return p->name[18]==b;
}
static void lift(struct panel *p,int i){
  struct contact *c=&p->c[i];
  if(!c->active) return;
  if(c->active==2){
    emit(p->fd,EV_ABS,ABS_MT_SLOT,i); emit(p->fd,EV_ABS,ABS_MT_TRACKING_ID,-1);
    if(heatlog()) fprintf(stderr,"heat %s ab   id %d nach %lld ms\n",p->name+18,c->id,now_ms()-c->t0);
  } else if(c->top && c->frames>=CONFIRM_FRAMES){
    /* DuoPort : appui bref en haut, levé avant la décision -> c'est un appui sur l'appli : on le rejoue juste sous la barre */
    double rx=c->x; { if(rx<300) rx=300-(300-rx)*etirement_x(p,0); else if(rx>PW-300) rx=(PW-300)+(rx-(PW-300))*etirement_x(p,1); }
    int x=(int)lround(rx); if(x<0)x=0; if(x>PW-1)x=PW-1;
    int id=next_id++&0xffff; if(!id) id=next_id++;
    emit(p->fd,EV_ABS,ABS_MT_SLOT,i); emit(p->fd,EV_ABS,ABS_MT_TRACKING_ID,id);
    emit(p->fd,EV_ABS,ABS_MT_POSITION_X,x); emit(p->fd,EV_ABS,ABS_MT_POSITION_Y,(int)TOP_APP_Y);
    emit(p->fd,EV_ABS,ABS_X,x); emit(p->fd,EV_ABS,ABS_Y,(int)TOP_APP_Y);
    if(!p->any) emit(p->fd,EV_KEY,BTN_TOUCH,1);
    emit(p->fd,EV_SYN,SYN_REPORT,0);
    emit(p->fd,EV_ABS,ABS_MT_SLOT,i); emit(p->fd,EV_ABS,ABS_MT_TRACKING_ID,-1);
    if(!p->any) emit(p->fd,EV_KEY,BTN_TOUCH,0);
    emit(p->fd,EV_SYN,SYN_REPORT,0);
    if(heatlog()) fprintf(stderr,"heat %s appui-haut rejoue x %d y %d\n",p->name+18,x,(int)TOP_APP_Y);
  } else if(heatlog()) fprintf(stderr,"heat %s geist verworfen: gitter %.1f,%.1f spitze %d flaeche %d\n",p->name+18,c->gx,c->gy,c->peak,c->area);
  c->active=0;
}
static void lift_all(struct panel *p){
  int any=0; for(int i=0;i<SLOTS;i++) if(p->c[i].active){ if(p->c[i].active==2) any=1; lift(p,i); }
  if(any){ emit(p->fd,EV_KEY,BTN_TOUCH,0); emit(p->fd,EV_SYN,SYN_REPORT,0); }
  p->any=0;
}

static int set_mode(int hid,int mode){
  unsigned char b[2]={0x05,(unsigned char)mode};
  return ioctl(hid,HIDIOCSFEATURE(2),b);
}

/* blobs of a frame: local maxima, centroid in the 3x3 (extended outward at the edge), palms rejected */
struct blob { double gx, gy; int peak, area; };
static int find_blobs(const unsigned char *h,struct blob *out,int max,int *cells){
  int s[GH*GW]; *cells=0;
  for(int k=0;k<GH*GW;k++){ int v=REST-h[k]; s[k]=v>0?v:0; if(s[k]>=T_KEEP) (*cells)++; }
  int n=0;
  for(int y=0;y<GH;y++) for(int x=0;x<GW;x++){
    int k=y*GW+x, v=s[k]; if(v<T_KEEP) continue;
    int is_max=1, area=0;
    for(int dy=-2;dy<=2;dy++) for(int dx=-2;dx<=2;dx++){
      int xx=x+dx, yy=y+dy; if(xx<0||yy<0||xx>=GW||yy>=GH) continue;
      int w=s[yy*GW+xx]; if(w>=T_KEEP) area++;
      if(dx>=-1&&dx<=1&&dy>=-1&&dy<=1&&(dx||dy)){ int kk=yy*GW+xx; if(w>v||(w==v&&kk<k)) is_max=0; }
    }
    if(!is_max) continue;
    if(area>PALM_CELLS) continue;
    double sw=0,sx=0,sy=0;
    for(int dy=-1;dy<=1;dy++) for(int dx=-1;dx<=1;dx++){
      int xx=x+dx, yy=y+dy; if(xx<0||yy<0||xx>=GW||yy>=GH) continue;
      double w=s[yy*GW+xx]-T_FLOOR; if(w<=0) continue; sw+=w; sx+=w*xx; sy+=w*yy;
    }
    double gx=sx/sw, gy=sy/sw;
    /* edge: half the blob lies outside the grid, the centroid slides inward -> push it outward,
     * the weaker the inner neighbour, the closer to the edge (the shell's edge swipe starts at the outermost edge) */
    if(x==0 && GW>1)    gx-=0.5*(1.0-(double)s[k+1]/v);
    if(x==GW-1)         gx+=0.5*(1.0-(double)s[k-1]/v);
    if(y==0 && GH>1)    gy-=0.5*(1.0-(double)s[k+GW]/v);
    if(y==GH-1)         gy+=0.5*(1.0-(double)s[k-GW]/v);
    int merged=0;
    for(int i=0;i<n;i++) if(hypot(out[i].gx-gx,out[i].gy-gy)<MERGE_CELLS){ if(v>out[i].peak){ out[i].gx=gx; out[i].gy=gy; out[i].peak=v; } merged=1; break; }
    if(!merged && n<max){ out[n].gx=gx; out[n].gy=gy; out[n].peak=v; out[n].area=area; n++; }
  }
  return n;
}

static void frame(struct panel *p,const unsigned char *h){
  p->last_frame=now_ms();
  if(!access(p->mute,F_OK)){ lift_all(p); return; }      /* posture service: back panel muted */
  struct blob b[16]; int cells; int n=find_blobs(h,b,16,&cells);
  if(cells>HAND_CELLS){ lift_all(p); return; }            /* a whole hand rests on it: report nothing */
  double bx[16], by[16]; int used[16]={0};
  for(int j=0;j<n;j++){
    bx[j]=p->cal[0]*b[j].gx+p->cal[1]*b[j].gy+p->cal[2];
    by[j]=p->cal[3]*b[j].gx+p->cal[4]*b[j].gy+p->cal[5];
  }
  for(int i=0;i<SLOTS;i++) p->c[i].seen=0;
  /* existing fingers: nearest blob (greedy, shortest pairs first) */
  for(;;){
    double best=TRACK_PX; int bi=-1,bj=-1;
    for(int i=0;i<SLOTS;i++) if(p->c[i].active&&!p->c[i].seen)
      for(int j=0;j<n;j++) if(!used[j]){ double d=hypot(bx[j]-p->c[i].x,by[j]-p->c[i].y); if(d<best){best=d;bi=i;bj=j;} }
    if(bi<0) break;
    p->c[bi].seen=1; used[bj]=1;
    struct contact *c=&p->c[bi];
    c->frames++; c->gx=b[bj].gx; c->gy=b[bj].gy; if(b[bj].peak>c->peak) c->peak=b[bj].peak; c->area=b[bj].area;
    /* smoothing: calm at rest, direct in motion (keep the lag small) */
    double d=hypot(bx[bj]-c->x,by[bj]-c->y), a=d>30?1.0:0.45+d/55.0;
    c->x+=a*(bx[bj]-c->x); c->y+=a*(by[bj]-c->y);
  }
  int changed=0;
  for(int i=0;i<SLOTS;i++) if(p->c[i].active&&!p->c[i].seen){ if(p->c[i].active==2) changed=1; lift(p,i); p->c[i].seen=-1; }
  /* report confirmed candidates now (touch-down point = first position, dead zone from there) */
  for(int i=0;i<SLOTS;i++){
    struct contact *c=&p->c[i];
    /* DuoPort : près du bord haut, le 1er relevé d'un doigt qui se pose est souvent tiré vers la rangée du bord
     * (mesuré : flèche « retour » à y≈125 relevée 1 fois sur 3 à y=51 -> barre de Phosh). On attend 2 trames de plus
     * et le point de départ devient la position stabilisée, pas la toute première. */
    int need = c->y < 260 ? CONFIRM_FRAMES + 2 : CONFIRM_FRAMES;
    if(c->active==1 && c->top){
      int drag = (c->y - c->sy) > TOP_DRAG_PX;
      if(!drag && c->frames < TOP_HOLD_FRAMES) continue;      /* on attend de savoir : glisser ou rester */
      if(!drag){ c->sx=c->x; c->sy=c->y; c->yoff = TOP_APP_Y - c->y; if(c->yoff<0) c->yoff=0; }
      /* glisser : on garde le point de départ en haut -> la barre de Phosh reçoit le geste ;
       * le tout premier point envoyé est EXACTEMENT ce départ (sinon il tombe déjà sous la barre) */
      if(drag) c->snap=1;
    } else if(c->active==1 && c->frames>=need){
      c->sx=c->x; c->sy=c->y;
      /* DuoPort 12/11 : doigt posé près d'un bord latéral -> point de départ = 1er relevé (sinon, glissé rapide,
       * le départ annoncé est déjà loin du bord et le geste « Retour » de lisgd ne se déclenche jamais) */
      if(c->fx<150 || c->fx>PW-150){ c->sx=c->fx; c->snap=1; }
      /* idem en haut / en bas de la dalle (bords latéraux vus en paysage) */
      if(c->fy<150 || c->fy>PH-150){ c->sy=c->fy; c->snap=1; }
    } else continue;
    {
      c->active=2; c->id=next_id++&0xffff; if(!c->id) c->id=next_id++;
      emit(p->fd,EV_ABS,ABS_MT_SLOT,i); emit(p->fd,EV_ABS,ABS_MT_TRACKING_ID,c->id); changed=1;
      if(heatlog()) fprintf(stderr,"heat %s auf  id %d gitter %.1f,%.1f px %.0f,%.0f spitze %d flaeche %d\n",p->name+18,c->id,c->gx,c->gy,c->sx,c->sy,c->peak,c->area);
    }
  }
  /* new fingers only with a clear signal; do not reuse slots freed in this frame right away
   * (Mutter crash 31.10.: lift + touch-down at the same instant on the same slot) */
  for(int j=0;j<n;j++) if(!used[j]&&b[j].peak>=T_ON){
    int slot=-1; for(int i=0;i<SLOTS;i++) if(!p->c[i].active&&p->c[i].seen!=-1){ slot=i; break; }
    if(slot<0) break;
    struct contact *c=&p->c[slot];
    *c=(struct contact){.active=1,.x=bx[j],.y=by[j],.sx=bx[j],.sy=by[j],.seen=2,.frames=1,.peak=b[j].peak,.area=b[j].area,
                        .gx=b[j].gx,.gy=b[j].gy,.t0=now_ms(),.top=by[j]<TOP_BAND&&barre_sur(p),.fx=bx[j],.fy=by[j]};
  }
  int first=-1, count=0;
  for(int i=0;i<SLOTS;i++){
    struct contact *c=&p->c[i]; if(c->active!=2) continue;
    count++;
    double ox=c->x, oy=c->y;
    if(c->snap){ ox=c->sx; oy=c->sy; c->snap=0; }
    else if(!c->slopped){ if(hypot(c->x-c->sx,c->y-c->sy)>=SLOP_PX){ c->slopped=1; c->dx=c->x-c->sx; c->dy=c->y-c->sy; } else { ox=c->sx; oy=c->sy; } }
    if(c->slopped){ c->dx*=0.5; c->dy*=0.5; ox=c->x-c->dx; oy=c->y-c->dy; }   /* ease off the dead zone's lag softly */
    oy += c->yoff;   /* DuoPort : appui immobile en haut décalé sous la barre */
    /* DuoPort : sur ce Duo la dernière rangée du capteur plafonne vers y=1745 -> étirer les 300 px du bas
     * pour atteindre la barre d'accueil de Phosh (bas de l'écran). Réglable : DUO_Y_STRETCH (défaut 1.25). */
    { static double k=0; if(!k){ const char *e=getenv("DUO_Y_STRETCH"); k=e?atof(e):1.25; if(k<1) k=1; }
      if(oy>PH-300) oy=(PH-300)+(oy-(PH-300))*k; }
    /* DuoPort 12/11 : sur les côtés, les colonnes du bord tirent le point vers l'intérieur (rien sous x≈80 :
     * le « + » en haut à gauche de Messages était inatteignable) -> étirer les 300 px de chaque côté.
     * Réglable : DUO_X_STRETCH (défaut 1.25). */
    { if(ox<300) ox=300-(300-ox)*etirement_x(p,0); else if(ox>PW-300) ox=(PW-300)+(ox-(PW-300))*etirement_x(p,1); }
    ox+=decalage(p,0); oy+=decalage(p,1);   /* calibration (duo-touch-test) */
    /* Phosh's home bar: full width, 15 logical px high, but only around the white handle (centred) should react.
     * A touch that starts in the bar outside the handle stays just above the bar for its whole life (goes to the
     * app). Only on the panel carrying Phosh's bars in normal orientation (/run/duo/barre). DUO_HOME_HALF_WIDTH
     * (logical px each side of the centre, default 100; 0 = whole bar as before) in /etc/duo/touch.conf. */
    { static double half=-1; if(half<0){ const char *e=getenv("DUO_HOME_HALF_WIDTH"); half=e?atof(e):100; }
      double band=15*ui_scale;
      if(!c->homechk){ c->homechk=1;
        if(half>0 && barre_sur(p) && oy>=PH-band && fabs(ox-PW/2.0)>half*ui_scale) c->homeblock=1; }
      if(c->homeblock && oy>PH-band-1) oy=PH-band-1; }
    int x=(int)lround(ox), y=(int)lround(oy);
    if(x<0)x=0; if(x>PW-1)x=PW-1; if(y<0)y=0; if(y>PH-1)y=PH-1;
    emit(p->fd,EV_ABS,ABS_MT_SLOT,i); emit(p->fd,EV_ABS,ABS_MT_POSITION_X,x); emit(p->fd,EV_ABS,ABS_MT_POSITION_Y,y);
    if(first<0){ first=i; emit(p->fd,EV_ABS,ABS_X,x); emit(p->fd,EV_ABS,ABS_Y,y); }
    changed=1;
  }
  if(count&&!p->any) emit(p->fd,EV_KEY,BTN_TOUCH,1);
  if(!count&&p->any) emit(p->fd,EV_KEY,BTN_TOUCH,0);
  p->any=count>0;
  if(changed) emit(p->fd,EV_SYN,SYN_REPORT,0);
}

int main(int argc,char**argv){
  charge_reglages();
  { const char *ch[]={"/r/etc/duo/scale","/etc/duo/scale"}; for(int k=0;k<2;k++){ FILE *f=fopen(ch[k],"r");
      if(f){ double v; if(fscanf(f,"%lf",&v)==1&&v>=1&&v<=4) ui_scale=v; fclose(f); break; } }
    fprintf(stderr,"duoheat: UI scale %.2f\n",ui_scale); }
  { const char *e=getenv("DUO_QUIET_MS"); if(e&&atoi(e)>=20) QUIET_MS=atoi(e); }
  const char *dev=argc>1?argv[1]:"/dev/hidraw0";
  struct panel L={.rid=0x3F,.pid=0x0c01,.name="Surface Duo Touch A",.mute="/run/duo/mute-A",.cal=CAL_L},
               R={.rid=0x0D,.pid=0x0c02,.name="Surface Duo Touch B",.mute="/run/duo/mute-B",.cal=CAL_R};
  int hid=open(dev,O_RDWR); if(hid<0){perror(dev);return 1;}
  if(mk_uinput(&L)||mk_uinput(&R)) return 1;
  mkdir("/run/duo",0755); mkdir("/run/duo/frei",01777); chmod("/run/duo/frei",01777);
  struct sigaction sa={0}; sa.sa_handler=on_term; sigaction(SIGTERM,&sa,NULL); sigaction(SIGINT,&sa,NULL);
  if(set_mode(hid,1)<0) perror("SET_FEATURE 05 01");
  fprintf(stderr,"duoheat: %s Heatmap-Modus, 0x3F -> %s, 0x0D -> %s\n",dev,L.name,R.name);
  static unsigned char b[8192]; long long last_switch=now_ms();
  struct panel *ps[2]={&L,&R};
  while(!term){
    struct pollfd pf={.fd=hid,.events=POLLIN};
    int r=poll(&pf,1,QUIET_MS/2);
    if(r<0){ if(errno==EINTR) continue; break; }
    long long t=now_ms();
    for(int i=0;i<2;i++) if(ps[i]->any && t-ps[i]->last_frame>QUIET_MS) lift_all(ps[i]);
    if(r==0) continue;
    int n=read(hid,b,sizeof b); if(n<=0){ if(errno==EINTR) continue; break; }
    if((b[0]==0x3F||b[0]==0x0D) && n>=HEAT_OFF+GW*GH){ frame(b[0]==0x3F?&L:&R,b+HEAT_OFF); continue; }
    /* fallback report = controller back in single-finger mode (restart/sleep): switch again like Microsoft's service */
    if((b[0]==0x40||b[0]==0x42) && t-last_switch>300){
      last_switch=t; fprintf(stderr,"duoheat: Notbericht 0x%02x, schalte neu\n",b[0]); set_mode(hid,1);
    }
  }
  lift_all(&L); lift_all(&R); usleep(100000);
  if(set_mode(hid,0)<0) perror("SET_FEATURE 05 00");
  ioctl(L.fd,UI_DEV_DESTROY); ioctl(R.fd,UI_DEV_DESTROY);
  fprintf(stderr,"duoheat: beendet, Notbetrieb wieder an\n");
  return 0;
}
