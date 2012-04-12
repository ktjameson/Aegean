#!/usr/bin/env python
"""
The Aegean source finding program.

Created by:
Paul Hancock
May 13 2011

Modifications by:
Paul Hancock
Jay Banyer
"""

#standard imports
import sys, os
import pyfits
import numpy as np
import math

#logging and nice options
import logging
from optparse import OptionParser

#external programs
from Imports.fits_image import FitsImage
from Imports.mpfit import mpfit
from Imports.convert import ra2dec, dec2dec, dec2hms, dec2dms

from scipy import ndimage as ndi
from scipy import stats

version='$Revision$'

#default header for all .fits images that I create
default_header="""BSCALE  =    1.00000000000E+00  /
BZERO   =    0.00000000000E+00  /
TELESCOP= 'SIM     '  /
CDELT1  =   -5.55555589756E-05  /
CRPIX1  =    2.56000000000E+02  /
CRVAL1  =    1.80000000000E+02  /
CTYPE1  = 'RA---SIN'  /
CDELT2  =   -5.55555589756E-05  /
CRPIX2  =    2.56000000000E+02  /
CRVAL2  =    0.00000000000E+00  /
CTYPE2  = 'DEC--SIN'  /
CELLSCAL= 'CONSTANT'  /
BUNIT   = 'JY/BEAM '  /
EPOCH   =    2.00000000000E+03  /
OBJECT  = 'none   '             /
OBSERVER= 'Various   '  /
VOBS    =    0.00000000000E+00  /
BTYPE   = 'intensity'  /
RMS     =    1        /"""

header="""#Aegean version {0}
# on dataset: {1}
#isl,src   bkg       rms         RA           DEC         RA         err         DEC        err         Peak      err     S_int     err        a    err    b    err     pa   err   flags
#         Jy/beam   Jy/beam                               deg        deg         deg        deg       Jy/beam   Jy/beam    Jy       Jy         ''    ''    ''    ''    deg   deg   NCPES
#=========================================================================================================================================================================================="""

# Set some bitwise logic for flood routines
PEAKED = 1    # 001
QUEUED = 2    # 010
VISITED = 4   # 100

# Err Flags for fitting routines
FITERRSMALL   = 1 #00001
FITERR        = 2 #00010
FIXED2PSF     = 4 #00100
FIXEDCIRCULAR = 8 #01000
NOTFIT        =16 #10000

####################################### CLASSES ##########################################

class Island():
    """
    A collection of pixels within an image.
    An island is generally composed of one or more components which are
    detected a characterised by Aegean

    Island(pixels,pixlist)
    pixels = an np.array of pixels that make up this island
    pixlist = a list of [(x,y,flux),... ] pixel values that make up this island
              This allows for non rectangular islands to be used. Pixels not listed
              are set to np.NaN and are ignored by Aegean.
    """
    def __init__(self,pixels=None,pixlist=None):
        if pixels is not None:
            self.pixels=pixels
        elif pixlist is not None:
            self.pixels=self.list2map(pixlist)
        else:
            self.pixels=self.gen_island(64,64)
            
    def __repr__(self):
        return "An island of pixels of shape {0},{1}".format(*self.pixels.shape)
        
    def list2map(self,pixlist):
        """
        Turn a list of (x,y) tuples into a 2d array
        returns: map[x,y]=self.pixels[x,y] and the offset coordinates
        for the new island with respect to 'self'.
        
        Input:
        pixlist - a list of (x,y) coordinates of the pixels of interest

        Return:
        pixels  - a 2d array with the pixels specified (and filled with NaNs)
        xmin,xmax,ymin,ymax - the boundaries of this box within self.pixels
        """
        xmin,xmax= min([a[0] for a in pixlist]), max([a[0] for a in pixlist])
        ymin,ymax= min([a[1] for a in pixlist]), max([a[1] for a in pixlist])
        pixels=np.ones(self.pixels[xmin:xmax+1,ymin:ymax+1].shape)*np.NaN
        for x,y in pixlist:
            pixels[x-xmin,y-ymin]=self.pixels[x,y]
        return pixels,xmin,xmax,ymin,ymax
            
    def map2list(self,map):
        """Turn a 2d array into a list of (val,x,y) tuples."""
        list=[(map[x,y],x,y) for x in range(map.shape[0]) for y in range(map.shape[1]) ]
        return list

    def get_pixlist(self,clip):
        # Jay's version
        indices = np.where(self.pixels > clip)
        ax, ay = indices
        pixlist = [(self.pixels[ax[i],ay[i]],ax[i],ay[i]) for i in range(len(ax))]
        return pixlist
        
    def gauss(self,a,x,fwhm):
        c=fwhm/(2*math.sqrt(2*math.log(2)))
        return a*math.exp( -x**2/(2*c**2) )

    def gen_island(self,nx,ny):
        """
        Generate an island with a single source in it
        Good for testing
        """
        fwhm_x=(nx/8)
        fwhm_y=(ny/4)
        midx,midy =math.floor(nx/2),math.floor(ny/2)    
        source=np.array( [[self.gauss(1,(x-midx),fwhm_x)*self.gauss(1,(y-midy),fwhm_y) for y in range(ny)]
                          for x in range(nx)] )
        source=source*(source>1e-3)
        return source

    def get_pixels(self):
        return self.pixels


class OutputSource():
    """
    Each source that is fit by Aegean is cast to this type.
    The parameters of the source are stored, along with a string
    formatter that makes printing easy. (as does OutputSource.__str__)
    """
    island = None # island number
    source = None # source number
    background = None # local background zero point
    local_rms= None #local image rms
    ra_str = None #str
    ra = None # degrees
    err_ra = None # degrees
    dec_str = None #str
    dec = None # degrees
    err_dec = None
    peak_flux = None # Jy/beam
    err_peak_flux = None # Jy/beam
    int_flux = None #Jy
    err_int_flux= None #Jy
    a = None # major axis (arcsecs)
    err_a = None # arcsecs
    b = None # minor axis (arcsecs)
    err_b = None # arcsecs
    pa = None # position angle (degrees - WHAT??)
    err_pa = None # degrees
    flags = None
    
    #formatting strings for making nice output
    formatter = "({0.island:04d},{0.source:d}) {0.background: 8.6f} {0.local_rms: 8.6f} "+\
                "{0.ra_str:12s} {0.dec_str:12s} {0.ra:11.7f} {0.err_ra: 9.7f} {0.dec:11.7f} {0.err_dec: 9.7f} "+\
                "{0.peak_flux: 8.6f} {0.err_peak_flux: 8.6f} {0.int_flux: 8.6f} {0.err_int_flux: 8.6f} "+\
                "{0.a:5.2f} {0.err_a:5.2f} {0.b:5.2f} {0.err_b:5.2f} "+\
                "{0.pa:6.1f} {0.err_pa:5.1f}   {0.flags:05b}\n"
    #format for kvis .ann files    
    ann_fmt_ellipse= "COLOUR green\nCIRCLE W {0.ra} {0.dec} 0.0083333333\n"
    ann_fmt_fixed= "COLOUR yellow\nCIRCLE W {0.ra} {0.dec} 0.0083333333\n"
    ann_fmt_fail= "COLOUR red\nCIRCLE W {0.ra} {0.dec} 0.0083333333\n"
    
    def __str__(self):
        return self.formatter.format(self)
    
    def as_list(self):
        """Return a list of all the parameters that are stored in this Source"""
        return [self.island,self.source,self.background,self.local_rms,
                self.ra_str, self.dec_str, self.ra,self.err_ra,self.dec,self.err_dec,
                self.peak_flux, self.err_peak_flux, self.int_flux, self.err_int_flux,
                self.a,self.err_a,self.b,self.err_b,
                self.pa,self.err_pa,self.flags]
    
class DummyMP():
    """
    A dummy copy of the mpfit class that just holds the parinfo variables
    This class doesn't do a great deal, but it makes it 'looks' like the mpfit class
    and makes it easy to estimate source parameters when you don't want to do any fitting.
    """

    def __init__(self,parinfo,perror):
        self.params=[]
        for var in parinfo:
            try:
                val=var['value'][0]
            except:
                val=var['value']
            self.params.append(val)
        self.perror=perror
        self.errmsg="There is no error, I just didn't bother fitting anything!"


######################################### FUNCTIONS ###############################

##pyfits load/save
def header_fill(header,info):
    """Take the header tokens from <info> and add them to <header>."""
    lines=info.split('\n')
    for l in lines:
        key,val = l[:-1].split('=')
        try:
            val=float(val)
        except ValueError:
            val=val.replace("'","").strip()
        header.update(key.strip(),val,'<=Defaulted')
    return

def load_pixels(filename,region=None):
    """
    Get a region of pixels from a .fits file <filename>
    region = [xmin,xmax,ymin,ymax]
    default is all pixels
    returns: pixels[y,x] as per pyfits format         
    """
    hdu=pyfits.open(filename)
    pixels=hdu[0].data
    if region is not None:
        if len(region)==4:
            c,d,a,b=region #pyfits loads arrays 'the other way'
            pixels=pixels[a:b,c:d]
    return pixels

def save(data,filename,header=default_header):
    """
    Save an np.array as a fits file <filename>[.fits]
    A default header is attached to the file so that it
    is a valid fits file. (and can be read in kvis/ds9)
    """
    hdu=pyfits.PrimaryHDU(data)
    hdulist=pyfits.HDUList()
    hdulist.append(hdu)
    header_fill(hdulist[0].header,header)
    hdulist.writeto("{0}.fits".format(filename),clobber=True)

## floodfill functions
def explore(data, rmsimg, status, queue, bounds, cutoffratio, pixel):
    """
    Look for pixels adjacent to <pixel> and add them to the queue
    Don't include pixels that are in the queue or that are below
    the cutoffratio
    
    This version requires an rms image to be present -PJH
    
    Returns: nothing
    """
    (x, y) = pixel
    if x < 0 or y < 0:
        print '\n WTF?! Just found a pixel at coordinate' , pixel
        print 'Something screwy going on, edge masking should have caught this.'
        print '*** Code terminating ***'
        sys.exit()

    if x > 0:
        new = (x - 1, y)
        if not status[new] & QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= QUEUED

    if x < bounds[0]:
        new = (x + 1, y)
        if not status[new] & QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= QUEUED

    if y > 0:
        new = (x, y - 1)
        if not status[new] & QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= QUEUED

    if y < bounds[1]:
        new = (x, y + 1)
        if not status[new] & QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= QUEUED

def flood(data, rmsimg, status, bounds, peak, cutoffratio):
    """
    Start at pixel=peak and return all the pixels that belong to
    the same blob.

    Returns: a list of pixels contiguous to <peak>

    This version requires an rms image - PJH
    """
    if status[peak] & VISITED:
        return []

    blob = []
    queue = [peak]
    status[peak] |= QUEUED

    for pixel in queue:
        if status[pixel] & VISITED:
            continue
    
        status[pixel] |= VISITED

        blob.append(pixel)
        explore(data, rmsimg, status, queue, bounds, cutoffratio, pixel)

    return blob

def gen_flood_wrap(data,rmsimg,innerclip,outerclip=None,expand=True):
    """
    <a generator function>
    Find all the sub islands in data.
    Detect islands with innerclip.
    Report islands with outerclip

    type(data) = Island
    return = [(pixels,xmin,ymin)[,(pixels,xmin,ymin)] ]
    where xmin,ymin is the offset of the subisland
    """
    if outerclip is None:
        outerclip=innerclip
        
    status=np.zeros(data.pixels.shape,dtype=np.uint8)
    # Selecting PEAKED pixels
    logging.debug("InnerClip: {0}".format(innerclip))

    status += np.where(data.pixels/rmsimg>innerclip,PEAKED,0)
    #logging.debug("status: {0}".format(status[1:5,1:5]))
    logging.debug("Peaked pixels: {0}/{1}".format(np.sum(status),len(data.pixels.ravel())))
    # making pixel list
    ax,ay=np.where(data.pixels/rmsimg>innerclip)
    peaks=[(data.pixels[ax[i],ay[i]],ax[i],ay[i]) for i in range(len(ax))]
    if len(peaks)==0:
        logging.info( "There are no pixels above the clipping limit")
        return
    # sorting pixel list
    peaks.sort(reverse=True)
    peaks=map(lambda x:x[1:],peaks)
    logging.debug("Brightest Peak {0}, SNR= {0}/{1}".format(data.pixels[peaks[0]],rmsimg[peaks[0]]))
    logging.debug("Faintest Peak {0}, SNR= {0}/{1}".format(data.pixels[peaks[-1]],rmsimg[peaks[-1]]))
    bounds=(data.pixels.shape[0]-1,data.pixels.shape[1]-1)
    
    # starting image segmentation
    for peak in peaks:
        blob=flood(data.pixels,rmsimg,status,bounds,peak,cutoffratio=outerclip)
        npix=len(blob)
        if npix>=1:#islands with no pixels have length 1
            if expand:
                logging.debug("I don't want to expand")
                logging.error("You said ''expand'' but this is not yet working!")
                sys.exit(1)
            new_isle,xmin,xmax,ymin,ymax=data.list2map(blob)
            if new_isle is not None:
                yield new_isle,xmin,xmax,ymin,ymax
    

   
##parameter estimates
def estimate_parinfo(data,rmsimg,curve,beam,csigma=None):
    """Estimates the number of sources in an island and returns initial parameters for the fit as well as
    limits on those parameters.

    input:
    data   - np.ndarray of flux values
    rmsimg - np.ndarray of 1sigmas values
    curve  - np.ndarray of curvature values
    beam   - beam object
    csigma - 1sigma value of the curvature map
             None => zero (default)

    returns:
    parinfo object for mpfit
    
    """
    #use a curvature of zero as a default significance cut
    if not csigma:
        csigma=0
    parinfo=[]
    
    #limits on how far the position is allowed to move based on the primary beam shape
    xo_lim=(beam.a*math.cos(beam.pa)+beam.b*math.sin(beam.pa))/2 
    yo_lim=(beam.a*math.sin(beam.pa)+beam.b*math.cos(beam.pa))/2
    fwhm2cc = 1/(2*math.sqrt(2*math.log(2)))
    logging.debug(" - shape {0}".format(data.shape))
    
    if not data.shape == curve.shape:
        logging.error("data and curvature are mismatched")
        logging.error("data:{0} curve:{1}".format(data.shape,curve.shape))
        sys.exit()

    #For small islands we can't do a 6 param fit
    #Don't count the NaN values as part of the island
    non_nan_pix=len(data[np.where(data==data)].ravel())
    if 4<= non_nan_pix and non_nan_pix <= 6:
        logging.debug("FIXED2PSF")
        is_flag=FIXED2PSF
    elif non_nan_pix < 4: 
        logging.debug("FITERRSMALL!")
        is_flag=FITERRSMALL
    else:
        is_flag=0
    logging.debug(" - size {0}".format(len(data.ravel())))

    if min(data.shape)<=2 or (is_flag & FITERRSMALL):
        #1d islands or small islands only get one source
        logging.debug("Tiny summit detected")
        logging.debug("{0}".format(data))
        summits=[ [data,0,data.shape[0],0,data.shape[1]] ]
    else:       
        kappa_sigma=Island( np.where( curve<-1*csigma, np.where(data-5*rmsimg>0, data,-1) ,-1) )
        summits=gen_flood_wrap(kappa_sigma,np.ones(kappa_sigma.pixels.shape),0,expand=False)
        
    i=0
    for summit,xmin,xmax,ymin,ymax in summits:
        
        summit_flag = is_flag
        logging.debug("Summit({5}) - shape:{0} x:[{1}-{2}] y:[{3}-{4}]".format(summit.shape,xmin,xmax,ymin,ymax,i))
        amp=summit[np.where(summit==summit)].max()#HAXORZ!! stupid NaNs break all my things
        logging.debug(" - max is {0}".format(amp))
        (xo,yo)=np.where(summit==amp)
        logging.debug(" - peak at {0},{1}".format(xo,yo))
        xo+=xmin
        yo+=ymin
        #allow amp to be 5% or 3sigma higher
        #TODO: the 5% should depend on the beam sampling
        amp_min,amp_max= float(4*rmsimg[xo,yo]), float(amp*1.05+3*rmsimg[xo,yo])
        logging.debug("a_min {0}, a_max {1}".format(amp_min,amp_max))
        
        xo_min,xo_max = min(xmin,xo-xo_lim),max(xmax,xo+xo_lim)
        if xo_min==xo_max: #if we have a 1d summit then allow the position to vary by +/-0.5pix
            xo_min,xo_max=xo_min-0.5,xo_max+0.5
        yo_min,yo_max = min(ymin,yo-yo_lim),max(ymax,yo+yo_lim)
        if xo_min==xo_max: #if we have a 1d summit then allow the position to vary by +/-0.5pix
            xo_min,xo_max=xo_min-0.5,xo_max+0.5

        #TODO: The limits on dx,dy work well for circular beams or unresolved sources
        #for elliptical beams *and* resolved sources this isn't good and should be redone

        #dx,dy are actually minor/major axes.
        #for a pa~0 dx lies along the x-axis and dy along the y-axis
        #initial shape is based on the size of the summit
        dx=max(beam.b,(xmax-xmin+1)*math.sqrt(2))*fwhm2cc
        #allow +1pix maximum size
        dx_min,dx_max = beam.b*fwhm2cc,max(dx,(xmax-xmin+1+1)*math.sqrt(2)*fwhm2cc) 
        dx_min=min(dx_min,dx_max) 
        
        dy=max(beam.a,(ymax-ymin+1)*math.sqrt(2))*fwhm2cc
        dy_min,dy_max = beam.a*fwhm2cc,max(dy,(ymax-ymin+1+1)*math.sqrt(2)*fwhm2cc)
        dy_min=min(dy_min,dy_max)

        #if the min/max of either major,minor are equal then use a PSF fit
        if dy_min==dy_max or dx_min==dx_max:
            summit_flag|=FIXED2PSF
            
        if summit_flag & FIXED2PSF:
            dx=beam.b*fwhm2cc
            dy=beam.a*fwhm2cc
            pa=beam.pa
        
        pa=0
        flag=summit_flag
        logging.debug(" - var val min max | min max")
        logging.debug(" - amp {0} {1} {2} ".format(amp,amp_min,amp_max))
        logging.debug(" - xo {0} {1} {2} ".format(xo,xo_min,xo_max))
        logging.debug(" - yo {0} {1} {2} ".format(yo,yo_min,yo_max))
        logging.debug(" - dx {0} {1} {2} | {3} {4}".format(dx,dx_min,dx_max,dx_min/fwhm2cc,dx_max/fwhm2cc))
        logging.debug(" - dy {0} {1} {2} | {3} {4}".format(dy,dy_min,dy_max,dy_min/fwhm2cc,dy_max/fwhm2cc))
        logging.debug(" - pa {0} {1} {2}".format(pa,-np.pi,np.pi))
        logging.debug(" - flags {0}".format(flag))
        parinfo.append( {'value':amp,
                         'fixed':False,
                         'parname':'{0}:amp'.format(i),
                         'limits':[amp_min,amp_max],
                         'limited':[True,True]} )
        parinfo.append( {'value':xo,
                         'fixed':False,
                         'parname':'{0}:xo'.format(i),
                         'limits':[xo_min,xo_max],
                         'limited':[True,True]} )
        parinfo.append( {'value':yo,
                         'fixed':False,
                         'parname':'{0}:yo'.format(i),
                         'limits':[yo_min,yo_max],
                         'limited':[True,True]} )
        #TODO - these flag==FIXED2PSF things need to be reconsidered
        parinfo.append( {'value':dx,
                         'fixed': (flag & FIXED2PSF)>0,
                         'parname':'{0}:dx'.format(i),
                         'limits':[dx_min,dx_max],
                         'limited':[True,True],
                         'flags':flag})
        parinfo.append( {'value':dy,
                         'fixed': (flag & FIXED2PSF)>0,
                         'parname':'{0}:dy'.format(i),
                         'limits':[dy_min,dy_max],
                         'limited':[True,True],
                         'flags':flag} )
        parinfo.append( {'value':pa,
                         'fixed': (flag & FIXED2PSF)>0,
                         'parname':'{0}:pa'.format(i),
                         'limits':[-np.pi,np.pi],
                         'limited':[False,False],
                         'flags':flag} )
        i+=1
    logging.debug("Estimated sources: {0}".format(i))
    return parinfo

def ntwodgaussian(inpars):
    """
    Return an array of values represented by multiple Gaussians as parameterized
    by params = [amp,x0,y0,dx,dy,pa]{n}
    
    A rewrite of gausfitter.twodgaussian to be faster or at least scale better.
    """
    if not len(inpars)%6 ==0:
        logging.error("Aww dude, wheres my parameters? You didn't give me enough!")
        sys.exit()
    pars=np.array(inpars).reshape(len(inpars)/6,6)
    amp=[a[0] for a in pars]
    xo=[ a[1] for a in pars]
    yo=[ a[2] for a in pars]
    dx=[ a[3] for a in pars]
    dy=[ a[4] for a in pars]
    #add pi/2 so that we are now East of North instead of South of East
    pa=[ a[5]+np.pi/2 for a in pars] 
    st=[ math.sin(p)**2 for p in pa]
    ct=[ math.cos(p)**2 for p in pa]
    s2t=[math.sin(2*p) for p in pa]
    a = [ (ct[i]/dx[i]**2 + st[i]/dy[i]**2)/2 for i in range(len(amp))]
    bb= [ s2t[i]/4 *(1/dy[i]**2-1/dx[i]**2) for i in range(len(amp))]
    c = [ (st[i]/dx[i]**2 + ct[i]/dy[i]**2)/2 for i in range(len(amp))]

    def rfunc(x,y):
        ans=0
        #list comprehension just breaks here, something to do with scope i think
        for i in range(len(amp)):
            ans+= amp[i]*np.exp(-1*(a[i]*(x-xo[i])**2 + 2*bb[i]*(x-xo[i])*(y-yo[i]) + c[i]*(y-yo[i])**2) )
        return ans
    return rfunc

def multi_gauss(data,rmsimg,parinfo):
    """
    Fit multiple gaussian components to data using the information provided by parinfo.
    data may contain 'flagged' or 'masked' data with the value of np.NaN
    input: data - pixel information
           rmsimg - image containing 1sigma values
           parinfo - initial parameters for mpfit
    return: mpfit object, parameter info
    """
    
    data=np.array(data)
    mask=np.where(data==data) #the indices of the *non* NaN values in data
    
    def model(p):
        """Return a map with a number of gaussians determined by the input parameters."""
        return ntwodgaussian(p)(*mask)
        
    def erfunc(p,fjac=None):
        """The difference between the model and the data"""
        return [0,np.ravel( (model(p)-data[mask] )/rmsimg[mask])]
    
    mp=mpfit(erfunc,parinfo=parinfo,quiet=True)

    #fix the major/minor axes so that maj>=min and correct the pa if needed
    for i in range(len(mp.params) /6):
        #fix the position angle so that it is East of North instead of South of East
        mp.params[i*6+5]+=np.pi/2        
        if mp.params[i*6+3]<mp.params[i*6+4]: # a<b
            #swap the major an minor axes (and the errors)
            bla=mp.params[i*6+4] 
            mp.params[i*6+4]=mp.params[i*6+3]
            mp.params[i*6+3]=bla
            if not(mp.perror is None):
                bla=mp.perror[i*6+4]
                mp.perror[i*6+4]=mp.perror[i*6+3]
                mp.perror[i*6+3]=bla
            #change the position angle
            mp.params[i*6+5]=mp.params[i*6+5]-np.pi/2
        #limit the range of pa from 0 to 2pi
        mp.params[i*6+5]-= int(mp.params[i*6+5]/(2*np.pi)) * 2*np.pi
        #now limit it to -pi to pi 
        if mp.params[i*6+5]>np.pi:
            mp.params[i*6+5]-=np.pi    
    
    return mp,parinfo
    

def make_bkg_rms_image(data,beam,mesh_size=20,forced_rms=None):
    """
    Calculate an rms image and a bkg image
    
    inputs:
    data - np.ndarray of flux values
    beam - beam object
    mesh_size - number of beams per box
                default = 20
    forced_rms - the rms of the image
                None => calculate the rms and bkg levels (default)
                <float> => assume zero background and constant rms

    return:
    bkgimg - np.ndarray of background offsets
    rmsimg - np.ndarray of 1 sigma levels
    """
    if forced_rms:
        return np.zeros(data.shape),np.ones(data.shape)*forced_rms
    
    img_y,img_x = data.shape
    xcen=int(img_x/2)
    ycen=int(img_y/2)

    width_x = mesh_size*int(max(abs(math.cos(beam.pa)*beam.b), abs(math.sin(beam.pa)*beam.a)))
    width_y = mesh_size*int(max(abs(math.sin(beam.pa)*beam.b), abs(math.cos(beam.pa)*beam.a)))
    
    rmsimg = np.zeros(data.shape)
    bkgimg = np.zeros(data.shape)
    logging.debug("image size x,y:{0},{1}".format(img_x,img_y))
    logging.debug("beam: {0}".format(beam))
    logging.debug("mesh width (pix) x,y: {0},{1}".format(width_x,width_y))

    #box centered at image center then tilling outwards
    xstart=(xcen-width_x/2)%width_x #the starting point of the first "full" box
    ystart=(ycen-width_y/2)%width_y
    
    xend=img_x - (img_x-xstart)%width_x #the end point of the last "full" box
    yend=img_y - (img_y-ystart)%width_y
      
    xmins=[0]
    xmins.extend(range(xstart,xend,width_x))
    xmins.append(xend)
    
    xmaxs=[xstart]
    xmaxs.extend(range(xstart+width_x,xend+1,width_x))
    xmaxs.append(img_x)
    
    ymins=[0]
    ymins.extend(range(ystart,yend,width_y))
    ymins.append(yend)
    
    ymaxs=[ystart]
    ymaxs.extend(range(ystart+width_y,yend+1,width_y))
    ymaxs.append(img_y)

    #if the image is smaller than our ideal mesh size, just use the whole image instead
    if width_x >=img_x:
        xmins=[0]
        xmaxs=[img_x]
    if width_y >=img_y:
        ymins=[0]
        ymaxs=[img_y]
    
    for xmin,xmax in zip(xmins,xmaxs):
        for ymin,ymax in zip(ymins,ymaxs):
            rms =    rms_from_iqr(data[ymin:ymax,xmin:xmax])
            bkg = bkg_from_middle(data[ymin:ymax,xmin:xmax])
            rmsimg[ymin:ymax,xmin:xmax] = rms
            bkgimg[ymin:ymax,xmin:xmax] = bkg
  
    return bkgimg,rmsimg

##Nifty helpers
def within(x,xm,xx):
    """Enforce xm<= x <=xx"""
    return min(max(x,xm),xx)

def rms_from_iqr(data):
    """Calculate the rms from the interquartile range"""
    p25 = stats.scoreatpercentile(data[np.where(data==data)].ravel(), 25)
    p75 = stats.scoreatpercentile(data[np.where(data==data)].ravel(), 75)
    iqr = p75 - p25
    return iqr / 1.34896

def bkg_from_middle(data):
    """Calculate the background level as the middle score"""
    mid = np.median(data[np.where(data==data)].ravel())
    return mid

def curvature(data,aspect=1.0):
    """Use a Lapacian kernal to figure the curvature map."""
    kern=np.array( [[1,1,1],[1,-8,1],[1,1,1]])
    #TODO: fiddle the weights of the above if we have a pixel aspect ratio that is not 1:1
    return ndi.convolve(data,kern)


########################################## TESTING ################################
# These were created at the same time as the parent functions but not updated
# they may therefore not work
######
def test_bkg_rms(data_file,temp_dir):
    print "TESTING RMS AND BGK IMGAES"
    print data_file
    img = FitsImage(data_file, hdu_index=0)
    data = Island(img.get_pixels())
    beam = img.beam
    bkgimg,rmsimg=make_bkg_rms_image(data.pixels,beam,mesh_size=20)
    save(bkgimg,'{0}/bkg_data'.format(temp_dir))
    save(rmsimg,'{0}/rms_data'.format(temp_dir))
    save(data.pixels,'{0}/data'.format(temp_dir))

def test_curvature(data_file,temp_dir):
    print "TESTING CURVATURE"
    data=load_pixels(data_file)
    curve=curvature(data)
    save(data,'{0}/tc_data'.format(temp_dir))
    save(curve,'{0}/tc_aplace'.format(temp_dir))
    save(d2x(data),'{0}/tc_d2x'.format(temp_dir))
    save(d2y(data),'{0}//tc_d2y'.format(temp_dir))
    lmask=(curve<0)*data
    dmask=(d2x(data)<0) * (d2y(data)<0) *data
    save(lmask,'{0}/tc_lmask'.format(temp_dir))
    save(dmask,'{0}/tc_dmask'.format(temp_dir))

######################################### THE MAIN DRIVING FUNCTION ###############
    
def find_sources_in_image(filename, hdu_index=0, outfile=None,rms=None, max_summits=None, csigma=None,innerclip=5,outerclip=4):
    """
    Run the Aegean source finder.
    Inputs:
    filename    - the name of the input file (FITS only)
    hdu_index   - the index of the FITS HDU (extension)
                   Default = 0
    outfile     - print the resulting catalogue to this file
                   Default = None = don't print to a file
    rms         - use this rms for the entire image (will also assume that background is 0)
                   default = None = calculate rms and background values
    max_summits - ignore any islands with more summits than this
                   Default = None = no limit
    csigma      - use this as the clipping limit for the curvature map
                   Default = None = calculate the curvature rms and use 1sigma
    innerclip   - the seeding clip, in sigmas, for seeding islands of pixels
                   Default = 5
    outerclip   - the flood clip in sigmas, used for flooding islands
                   Default = 4
    Return:
    a list of OutputSource objects
    """
    img = FitsImage(filename, hdu_index=hdu_index)
    data = Island(img.get_pixels())
    dcurve=curvature(img.get_pixels())    

    beam=img.beam

    bkgimg,rmsimg = make_bkg_rms_image(data.pixels,beam,mesh_size=20,forced_rms=rms)
    
    if csigma is None:
        csigma=rms_from_iqr(dcurve)
    
    #TODO: don't assume square pixels. This will definately mean the source
    # parameters are a bit wrong for images where degrees-per-pixel is not equal in X and Y
    pix2arcsec = abs(img.deg_per_pixel_x) * 3600
    logging.info("pix2arcsec={0} beam={1.a:5.2f}x{1.b:5.2f}, {1.pa:5.2e}".format(pix2arcsec, beam))
    logging.info("csigma={0}".format(csigma))
    logging.info("seedclip={0}".format(innerclip))
    logging.info("floodclip={0}".format(outerclip))
    cc2fwhm = (2*math.sqrt(2*math.log(2)))
    cc2arcsec=cc2fwhm*pix2arcsec
    
    num_isle=0

    sources = []
    if outfile:
        print >>outfile,header.format(version,filename)
    for i,xmin,xmax,ymin,ymax in gen_flood_wrap(data,rmsimg,innerclip,outerclip,expand=False):
        if len(i)<=1:
            #empty islands have length 1
            continue 
        logging.debug("=====")
        logging.debug("Island ({0})".format(num_isle) )
        num_isle+=1
        isle=Island(i)
        icurve = dcurve[xmin:xmax+1,ymin:ymax+1]
        rms=rmsimg[xmin:xmax+1,ymin:ymax+1]
        bkg=bkgimg[xmin:xmax+1,ymin:ymax+1]
        parinfo= estimate_parinfo(isle.pixels,rms,icurve,beam,csigma)

        logging.debug("Rms is {0}".format(np.shape(rms)) )
        logging.debug("Isle is {0}".format(np.shape(isle.pixels)) )
        logging.debug(" of which {0} are masked".format(sum(np.isnan(isle.pixels).ravel()*1)))
        
        # skip islands with too many summits (Gaussians)
        num_summits = len(parinfo) / 6 # there are 6 params per Guassian
        logging.debug("max_summits, num_summits={0},{1}".format(max_summits,num_summits))
        
        #extract a flag for the island
        is_flag=0
        for src in parinfo:
            if src['parname'].split(":")[-1] in ['dy','dx','pa']:
                if src['flags']& FITERRSMALL:
                    is_flag=src['flags']
                    break
        if max_summits is not None and num_summits > max_summits:
            logging.info("Island has too many summits ({0}), not fitting anything".format(num_summits))
            #set all the flags to be NOTFIT
            for src in parinfo:
                if src['parname'].split(":")[-1] in ['dy','dx','pa']:
                    src['flags']|=NOTFIT
            mp=DummyMP(parinfo=parinfo,perror=None)
            info=parinfo
        if is_flag & FITERRSMALL:
            logging.debug("Island is too small for a fit, not fitting anything")
            #set all the flags to be NOTFIT
            for src in parinfo:
                if src['parname'].split(":")[-1] in ['dy','dx','pa']:
                    src['flags']|=NOTFIT
            mp=DummyMP(parinfo=parinfo,perror=None)
            info=parinfo
            
        else:
            mp,info=multi_gauss(isle.pixels,rms,parinfo)
            
        params=mp.params
        #report the source parameters
        err=False
        for j in range(len(params)/6):
            source = OutputSource()
            source.island = num_isle
            source.source = j
            
            flags=0
            if mp.perror is None:
                mp.perror = [0 for a in mp.params]
                err=True
                logging.debug("FitError: {0}".format(mp.errmsg))
                
                logging.debug("info = {0}".format(info))
            for k in range(len(mp.perror)):
                if mp.perror[k]==0.0:
                    mp.perror[k]=-1
            if err:
                flags|=FITERR
            #read the flag information from the 'pa'
            flags|= info[j*6+5]['flags']
            
            #np.float32 has some stupid problem with str.format so i have to cast everything to a float64
            mp.params=[np.float64(a) for a in mp.params]
            
            #params = [amp,x0,y0,dx,dy,pa]{n}
            #pixel pos within island + 
            # island offset within region +
            # region offset within image +
            # 1 for luck
            # (pyfits->miriad conversion = luck)
            dec_pix=mp.params[j*6+1] + xmin + 1
            ra_pix =mp.params[j*6+2] + ymin + 1
            coords = img.pix2sky([ra_pix, dec_pix])
            source.ra = coords[0]
            source.dec = coords[1]
            source.ra_str= dec2hms(source.ra)
            source.dec_str= dec2dms(source.dec)
            
            #calculate ra,dec errors from the pixel error
            if mp.perror[j*6+1]<0:
                source.err_ra =-1
                source.err_dec=-1
            else:
                #really big errors cause problems
                #limit the errors to be the width of the island
                dec_err_pix=dec_pix + within(mp.perror[j*6+1],-1,i.shape[1])
                ra_err_pix =ra_pix + within(mp.perror[j*6+2],-1,i.shape[0])
                err_coords = img.pix2sky([ra_err_pix, dec_err_pix])
                source.err_ra = abs(source.ra - err_coords[0])
                source.err_dec = abs(source.dec - err_coords[1])

            # flux values
            #the background is taken from background map
            # Clamp the pixel location to the edge of the background map (see Trac #51)
            x = max(min(int(round(ra_pix)), bkgimg.shape[1]-1),0)
            y = max(min(int(round(dec_pix)), bkgimg.shape[0]-1),0)
            source.background=bkgimg[y,x]
            source.local_rms=rmsimg[y,x]
            source.peak_flux = mp.params[j*6]
            source.err_peak_flux = mp.perror[j*6]
            
            # major/minor axis and position angle
            source.a = mp.params[j*6+3]*cc2arcsec
            source.err_a = mp.perror[j*6+3]
            if source.err_a>0:
                source.err_a*=cc2arcsec
            source.b = mp.params[j*6+4]*cc2arcsec
            source.err_b = mp.perror[j*6+4]
            if source.err_b>0:
                source.err_b*=cc2arcsec
            source.pa = mp.params[j*6+5]*180/np.pi
            source.err_pa = mp.perror[j*6+5]
            if source.err_pa>0:
                source.err_pa*=180/np.pi
            source.flags = flags

            #integrated flux is calculated not fit or measured
            source.int_flux=source.peak_flux*source.a*source.b/(beam.a*beam.b*pix2arcsec**2)
            source.err_int_flux=source.int_flux*math.sqrt( (source.err_peak_flux/source.peak_flux)**2
                                                         +(source.err_a/source.a)**2
                                                         +(source.err_b/source.b)**2)
            
            
            sources.append(source)
            
            if outfile:
                outfile.write(source.formatter.format(source)[:-1])
                outfile.write("\n")

            logging.debug(source.formatter.format(source)[:-1])
    return sources
    
def save_background_files(image_filename, hdu_index=0):
    '''
    Generate and save the background and RMS maps as FITS files.
    They are saved in the current directly as aegean-background.fits and aegean-rms.fits.
    '''
    logging.info("Saving background / RMS maps")
    img = FitsImage(image_filename, hdu_index=hdu_index)
    data = img.get_pixels()
    beam=img.beam
    bkgimg,rmsimg = make_bkg_rms_image(data,beam,mesh_size=20)
    
    # Generate the new FITS files by copying the existing HDU and assigning new data.
    # This gives the new files the same WCS projection and other header fields. 
    new_hdu = img.hdu
    # Set the ORIGIN to indicate Aegean made this file
    new_hdu.header.update("ORIGIN", "Aegean {0}".format(version))
    new_hdu.data = bkgimg
    new_hdu.writeto("aegean-background.fits", clobber=True)
    new_hdu.data = rmsimg
    new_hdu.writeto("aegean-rms.fits", clobber=True)
    logging.info("Saved aegean-background.fits and aegean-rms.fits")
    
if __name__=="__main__":
    usage="usage: %prog [options] FileName.fits"
    parser = OptionParser(usage=usage)
    parser.add_option("--debug", dest="debug", action="store_true",
                      help="Enable debug log output")
    parser.add_option("--hdu", dest="hdu_index", type="int",
                      help="HDU index (0-based) for cubes with multiple images in extensions")
    parser.add_option("--outfile",dest='outfile',
                      help="Destination of catalog output, default=stdout")
    parser.add_option("--rms",dest='rms',type='float',
                      help="Assume a single image noise of rms, default is to calculate a rms over regions of 20x20 beams")
    parser.add_option("--maxsummits",dest='max_summits',type='float',
                      help="If more than *maxsummits* summits are detected in an island, no fitting is done, only estimation. Default is None = always fit")
    parser.add_option("--csigma",dest='csigma',type='float',
                      help="The clipping value applied to the curvature map, when deciding which peaks/summits are significant. Default is None = calculate from image")
    parser.add_option('--seedclip',dest='innerclip',type='float',
                     help='The clipping value (in sigmas) for seeding islands. Default=5')
    parser.add_option('--floodclip',dest='outerclip',type='float',
                      help='The clipping value (in sigmas) for growing islands. Default=4')
    parser.add_option('--file_version',dest='file_version',action="store_true",
                      help='show the versions of each file')
    parser.add_option('--save_background', dest='save_background', action="store_true",
                      help='save the background/rms maps to aegean-background.fits, aegean-rms.fits and exit')
    parser.set_defaults(debug=False,hdu_index=0,outfile=sys.stdout,rms=None,max_summits=None,csigma=None,innerclip=5,outerclip=4,file_version=False)
    (options, args) = parser.parse_args()

    # configure logging
    logging_level = logging.DEBUG if options.debug else logging.INFO
    logging.basicConfig(level=logging_level)
    logging.info("This is Aegean {0}".format(version))
    if options.file_version:
        logging.info("Using aegean.py {0}".format(version))
        logging.info("Using fits_image.py {0}".format(FitsImage.version))
        sys.exit()
        
    if len(args)==0:
        parser.print_help()
        sys.exit()
    filename = args[0]
    if not os.path.exists(filename):
        logging.error( "{0} does not exist".format(filename))
        sys.exit()
    hdu_index = options.hdu_index
    if hdu_index > 0:
        logging.info( "Using hdu index {0}".format(hdu_index))
        
    # Generate and save the background FITS files and exit if requested
    if options.save_background:
        save_background_files(filename, hdu_index=hdu_index)
        sys.exit()
        
    #Open the outfile
    if options.outfile is not sys.stdout:
        options.outfile=open(os.path.expanduser(options.outfile),'w')
    
    sources = find_sources_in_image(filename, outfile=options.outfile, hdu_index=options.hdu_index,rms=options.rms,max_summits=options.max_summits,csigma=options.csigma,innerclip=options.innerclip,outerclip=options.outerclip)

