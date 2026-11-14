export type ThermalPalette = 'ironbow' | 'white' | 'black';

/** Apply a palette only to pixels with measured temperature. Cloud/no-data alpha stays transparent. */
export function colorizeThermal(data: Uint8ClampedArray, palette: ThermalPalette): Uint8ClampedArray {
  const out=new Uint8ClampedArray(data.length);
  const stops=[[3,0,10],[33,5,97],[120,13,153],[209,56,82],[247,133,15],[255,219,92],[255,255,235]];
  for(let i=0;i<data.length;i+=4){
    const value=data[i];out[i+3]=data[i+3];
    if(!data[i+3])continue;
    if(palette!=='ironbow'){const v=palette==='white'?value:255-value;out[i]=out[i+1]=out[i+2]=v;continue;}
    const t=value/255*(stops.length-1),a=Math.min(stops.length-2,Math.floor(t)),f=t-a;
    for(let k=0;k<3;k++)out[i+k]=Math.round(stops[a][k]*(1-f)+stops[a+1][k]*f);
  }
  return out;
}
