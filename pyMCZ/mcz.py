#!/usr/bin/env python
import os,sys,argparse,warnings

import numpy as np
import scipy.stats as stats
from scipy.special import gammaln
from scipy import optimize
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter


#modules of this package
import pylabsetup

#import metallicity_save2 as metallicity
import metallicity as metallicity
reload(metallicity)
import metscales as ms

import itertools
import multiprocessing as mpc
NM0=0# setting this to say N>0 starts the calculation at measurement N.
#this is only for exploratory purposes as the code bugs out before
#plotting and printing the results

PROFILING = True
PROFILING = False

from astropy import table as t, constants as c, units as u, wcs
from astropy.visualization import hist
import corner


alllines=['[OII]3727','Hb','[OIII]4959','[OIII]5007','[OI]6300','Ha','[NII]6584','[SII]6717','[SII]6731','[SIII]9069','[SIII]9532']
morelines=['E(B-V)','dE(B-V)','scale_blue','d scale_blue']

MAXPROCESSES=10

#pickle may not be installed
NOPICKLE=False
try:
    import pickle
except ImportError:
    NOPICKLE=True


CLOBBER=False
VERBOSE=False
UNPICKLE=False
ASCIIOUTPUT=False
ASCIIDISTRIB=False
RUNSIM=True
NOPLOT=False
BINMODE='k'
binning={'bb':'Bayesian blocks','k':"Knuth's rule",'d':"Doane's formula",'s':r'$\sqrt{N}$','t':r'$2 N^{1/3}$', 'kd':'Kernel Density'}

MP=False

def is_number(s):
    if not type(s) is np.string_:
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False

def smart_open(logfile=None):
    if logfile and logfile != '-':
        fh = open(logfile, 'w')
    else:
        fh = sys.stdout
    return fh

def getknuth(m,data,N):
    m=int(m)
    if m > N:
        return [-1]
    bins=np.linspace(min(data),max(data), int(m) + 1)
    try:
        nk,bins=np.histogram(data,bins)
        return -(N*np.log(m) + gammaln(0.5*m) - m*gammaln(0.5) - gammaln(N + 0.5*m)+ np.sum(gammaln(nk+0.5)))
    except:
        return [-1]

def knuthn(data, maxM=None):
    assert data.ndim==1, "data must be 1D array to calculate Knuth's number of bins"
    N=data.size
    if not maxM:
        maxM=5*np.sqrt(N)
    m0=2.0*N**(1./3.)
    gk=getknuth
    if gk == [-1]:
        return m0, 't'
    mkall= optimize.fmin(gk,m0, args=(data,N), disp=VERBOSE, maxiter=30)#, maxfun=1000)#[0]
    mk=mkall[0]
    if mk>maxM or mk<0.3*np.sqrt(N):
        mk=m0
        return mk, 't'
    return mk, 0

##############################################################################
##The input data
##############################################################################
##Reads the flux file and returns it as an array.
##Ignores non-numeric lines
##Returns  (flux array,num)
##############################################################################
def readfile(filename):
    noheader=1
    findex=-1
    f=open(filename,'r')
    l0=f.readline().replace(' ','')
    l1=f.readline().split()
    if l0.startswith('#') or l0.startswith(';'):
        header=l0.strip().replace(";",'').replace("#",'').split(',');
        header[0]=header[0].replace(' ','')
        header=header[:len(l1)]
    else:
        noheader=0
        header=['galnum']+alllines+['flag']+morelines
        header=header[:len(l1)]

    formats=['S10']+['f']*(len(header)-1)
    if 'flag' in header:
        findex=header.index('flag')
        formats[findex]='S10'

    bstruct={}
    for i,k in enumerate(header):
        bstruct[k]=[i,0]
    b = np.loadtxt(filename,skiprows=noheader, dtype={'names':header,'formats':formats}, comments=';')
    if b.size == 1:
        b=np.atleast_1d(b)

    for i,k in enumerate(header):
        if not k=='flag' and is_number(b[k][0]):
            bstruct[k][1]=np.count_nonzero(b[k])+sum(np.isnan(b[k]))
    j=len(b['galnum'])
    return b, j, bstruct

def ingest_data(filename,path):
    ###Initialize###
    measfile = os.path.join(path, filename + "_meas.txt")
    errfile  = os.path.join(path, filename + "_err.txt")

    ###read the max, meas, min flux files###
    meas, nm, bsmeas = readfile(measfile)
    err,  nm, bserr  = readfile(errfile)
    try:
        snr=(meas[:,1:].view(np.float32).reshape(meas[:,1:].shape + (-1,))[:,1:])/(err[:,1:].view(np.float32).reshape(err[:,1:].shape + (-1,))[:,1:])
        if snr[~np.isnan(snr)].any()<3:
            raw_input('''WARNING: signal to noise ratio smaller than 3
            for at least some lines! You should only use SNR>3
            measurements (return to proceed)''')
    except (IndexError, TypeError):
        pass
    return (filename, meas, err, nm, path, (bsmeas,bserr))

def input_data(filename,path):
    p = os.path.join(path,"input")
    assert os.path.isdir(p), "bad data directory %s"%p
    if os.path.isfile(os.path.join(p,filename+'_err.txt')):
        if os.path.isfile(os.path.join(p,filename+'_meas.txt')):
            return ingest_data(filename,path=p)
    print "Unable to find _meas and _err files ",filename+'_meas.txt',filename+'_err.txt',"in directory ",p
    return -1



##############################################################################
##returns appropriate bin size for the number of data
##mode 'k' calculates this based on Knuth's rule
##mode 'd' calculates this based on Doane's formula
##mode 's' calculates this based on sqrt of number of data
##mode 't' calculates this based on 2*n**1/3 (default)
##############################################################################
def getbinsize(n,data,):
    if BINMODE=='d':
        g1=np.abs(stats.mstats.moment(data,moment=3))#/data.std())
        s1=np.sqrt(float(n)/6.0)
        #s1=1.0/np.sqrt(6.*(n-2.)/((n+1.)*(n+3.)))
        k=1+np.log2(n)+np.log2(1+(g1*s1)),0
    elif BINMODE=='s':
        k=np.sqrt(n),0
    elif BINMODE=='t':
        k=2.*n**(1./3.),0
    else:
        k= knuthn(data)
    return k


##############################################################################
##Check if hist files already exist and need to be replaced
##############################################################################
def checkhist(snname,Zs,nsample,i,path):
    global CLOBBER

    name='%s_n%d_%s_%d'%((snname,nsample,Zs,i+1))
    outdir=os.path.join(path,'hist')
    outfile=os.path.join(outdir,name+".pdf")
    if os.path.isfile(outfile) and not CLOBBER:
        replace=raw_input("replacing existing  image files, starting with: %s ? [Y/n]\n"%outfile).lower()
        assert(not (replace.startswith('n'))),"save your existing output directory under another name first"
        CLOBBER =True

##############################################################################
##Save the result as histogram as name
##############################################################################
#@profile
def savehist(data,snname,Zs,nsample,i,path,nmeas,measnames, verbose=False, fs=24):
    global BINMODE
    #global NOPLOT

    name='%s_n%d_%s_%d'%((snname,nsample,Zs,i+1))
    outdir=os.path.join(path,'hist')
    outfile=os.path.join(outdir,name+".pdf")
    if not NOPLOT:
        fig=plt.figure(figsize=(11,8))
        plt.clf()

    ####kill outliers, infinities, and bad distributions###
    data=data[np.isfinite(data)]

    n=data.shape[0]
    kde=None

    if not n>0:
        if verbose:print "data must be an actual distribution (n>0 elements!, %s)"%Zs
        return "-1,-1,_1",[], kde

    if data.shape[0]<=0 or np.sum(data)<=0:
        print '{0:15} {1:20} {2:>13d}   {3:>7d}   {4:>7d} '.format(snname,Zs,-1,-1,-1)
        return "-1, -1, -1",[], kde
    try:
        ###find C.I.###
        median,pc16,pc84=np.percentile(data,[50,16,84])
        std=np.std(data)
        left=pc16
        right=pc84
        maxleft=median-std*5
        maxright=median+std*5
        if "%2f"%maxright=="%2f"%maxleft:
            maxleft=median-1
            maxright=median+1
        if round(right,6)==round(left,6) and round(left,6)==round(median,6):
            print '{0:15} {1:20} {2:>13.3f}   -{3:>7.3f}   +{4:>7.3f} (no distribution)'.format(snname,Zs,median,0,0 )

            return "%f\t %f\t %f"%(round(median,3), round(median-left,3), round(right-median,3)), data,kde#"-1,-1,-1",[]
        ###print out the confidence interval###
        print '{0:15} {1:20} {2:>13.3f}   -{3:>7.3f}   +{4:>7.3f}'.format(snname, Zs, round(median,3), round(median-left,3), round(right-median,3))
        alpha=1.0

        ######histogram######
        if BINMODE=='kd':
            ##if sklearn is available use it to get Kernel Density
            try:
                from sklearn.neighbors import KernelDensity
            except ImportError:
                print '''sklearn is not available,
                thus we cannot compute kernel density.
                switching to bayesian blocks'''
                BINMODE='bb'
        if BINMODE=='kd':
            ##bw is chosen according to Silverman 1986
            bw=1.06*std*n**(-0.2)
            numbin,bm=getbinsize(data.shape[0],data)
            distrib=np.histogram(data, bins=int(numbin), density=True)
            ###make hist###
            counts, bins=distrib[0],distrib[1]
            widths=np.diff(bins)
            countsnorm=counts/np.max(counts)

            if bw >0:
                kde = KernelDensity(kernel='gaussian', bandwidth=bw).fit(data[:, np.newaxis])
                kdebins=np.linspace(maxleft,maxright,1000)[:, np.newaxis]
                log_dens = kde.score_samples(kdebins)
                dens=np.exp(log_dens)
                norm=countsnorm.sum()*(bins[1]-bins[0])/dens.sum()/(kdebins[1]-kdebins[0])
                if not NOPLOT: plt.fill(kdebins[:,0], dens*norm, fc='#7570b3', alpha=0.8)
                alpha=0.5

        ###find appropriate bin size###
        else:
            if BINMODE=='bb' :
                ##if astroML is available use it to get Bayesian blocks
                bm = 0
                try:
                    from astroML.plotting import hist as amlhist
                    if BINMODE=='bb':
                        distrib=amlhist(data, bins='blocks', normed=True)
                    if not NOPLOT: plt.clf()
                except ImportError:
                    print "bayesian blocks for histogram requires astroML to be installed"
                    print "defaulting to Knuth's rule "
                    ##otherwise
                    numbin,bm=getbinsize(data.shape[0],data)
                    distrib=np.histogram(data, bins = int(numbin), density=True)
            else:
                numbin,bm=getbinsize(data.shape[0],data)
                distrib=np.histogram(data, numbin, density=True)

            ###make hist###
            counts, bins=distrib[0],distrib[1]
            widths=np.diff(bins)
            countsnorm=counts/np.max(counts)

        ###plot hist###
        if NOPLOT: return "%f\t %f\t %f"%(round(median,3), round(median-left,3), round(right-median,3)), data,kde

        plt.bar(bins[:-1],countsnorm,widths,color=['gray'], alpha=alpha)
        plt.minorticks_on()
        plt.gca().xaxis.set_major_formatter(FormatStrFormatter('%.2f'))
        plt.xlim(maxleft,maxright)

        #the following lines assure the x tick label is
        #within the length of the x axis
        xticks=plt.xticks()[0]
        dx=xticks[-1]-xticks[-2]
        xticks=xticks[(xticks<maxright)*(xticks>maxleft)]
        if (maxright-xticks[-1])<0.25*dx:
            maxright=maxright+0.25*dx
            maxleft =maxleft -0.25*dx
        plt.xlim(maxleft,maxright)
        plt.xticks(xticks, ['%.2f'%s for s in xticks])

        plt.ylim(0,1.15)
        plt.yticks(np.arange(0.2,1.3,0.2 ), [ "%.1f"%x for x in np.arange(0.2,1.1,0.2)])
        plt.axvspan(left,right,color='DarkOrange',alpha=0.4)
        plt.axvline(x=median,linewidth=2,color='white',ls='--')

        #labels and legends
        st='%s '%(snname)
        plt.annotate(st, xy=(0.13, 0.6), xycoords='axes fraction',size=fs,fontweight='bold')
        st='%s '%(Zs.replace('_',' '))
        plt.annotate(st, xy=(0.61, 0.93), xycoords='axes fraction',fontsize=fs,fontweight='bold')
        st='measurement %d of %d\n   %s\nmedian: %.3f\n16th Percentile: %.3f\n84th Percentile: %.3f'%(i+1,nmeas,measnames[i],round(median,3),round(left,3),round(right,3))
        plt.annotate(st, xy=(0.61, 0.65), xycoords='axes fraction',fontsize=fs)
        effectiven=len(data[~np.isnan(data)])
        if effectiven:
            st='MC sample size %d (%d)\nhistogram rule: %s'%(effectiven,nsample,binning[BINMODE])
        if bm:
            if effectiven<nsample:
                st='MC sample size %d (%d)\nhistogram rule: %s'%(effectiven,nsample,binning[bm])
            else:
                st='MC sample size %d\nhistogram rule: %s'%(nsample,binning[bm])
        plt.annotate(st, xy=(0.61, 0.55), xycoords='axes fraction',fontsize=fs-5)
        if "E(B-V)" in Zs:
            plt.xlabel('E(B-V) [mag]')
            outfile=outfile.replace('(','').replace(')','')
        elif "logR23" in Zs:
            plt.xlabel('logR23')
        else:
            plt.xlabel('12+log(O/H)')
        plt.ylabel('relative counts')
        plt.savefig(outfile,format='pdf')
        plt.close(fig)


        return "%f\t %f\t %f"%(round(median,3), round(median-left,3), round(right-median,3)), data,kde

    except (OverflowError,AttributeError,ValueError):
        if VERBOSE: print data
        print name, 'had infinities (or something in plotting went wrong)'
        return "-1, -1,-1",[],None


def calc((i,(sample,flux,err,nm,bss,mds,disp, dust_corr,verbose,res,scales,nps, logf))):
    logf=sys.stdout
    print >>logf,"\n\nreading in measurements ",i+1
    fluxi={}#np.zeros((len(bss[0]),nm),float)
    for k in bss[0].iterkeys():
        print >>logf,'{0:15} '.format(k),
        print >>logf,'{0:0.2} +/- {1:0.2}'.format(flux[k][i],err[k][i])
        fluxi[k]=flux[k][i]*np.ones(len(sample[i]))+err[k][i]*sample[i]
        warnings.filterwarnings("ignore")
    success=metallicity.calculation(scales[i],fluxi,nm,mds,nps,logf,disp=disp, dust_corr=dust_corr,verbose=verbose)
    if success==-1:
        print >>logf, "MINIMUM REQUIRED LINES: '[OII]3727','[OIII]5007','[NII]6584','[SII]6717'"

    for key in scales[i].mds.iterkeys():
        res[key][i]=scales[i].mds[key]
        if res[key][i] is None:
            res[key][i]=[float('NaN')]*len(sample)
    return res

##############################################################################
##  The main function. takes the flux and its error as input.
##  filename - a string 'filename' common to the three flux files
##  flux - np array of the fluxes
##  err - the flux errors, must be the same dimension as flux
##  nsample - the number of samples the code will generate. Default is 100
##  errmode - determines which method to choose the bin size.
##      mode 'k' calculates this based on Knuth's rule (default)
##      mode 'd' calculates this based on Doane's formula
##      mode 's' calculates this based on sqrt of number of data
##      mode 't' calculates this based on 2*n**1/3
##############################################################################
#@profile
def run((name, flux, err, nm, path, bss), nsample, mds, multiproc, logf, unpickle=False, dust_corr=True,verbose=False, fs=24):
    global RUNSIM#,BINMODE#,NOPLOT
    assert(len(flux[0])== len(err[0])), "flux and err must be same dimensions"
    assert(len(flux['galnum'])== nm), "flux and err must be of declaired size"
    assert(len(err['galnum'])== nm), "flux and err must be same dimensions"


    #increasing sample by 10% to assure robustness against rejected samples
    newnsample=nsample
    if nsample>1: newnsample=int(nsample+0.1*nsample)

    p=os.path.join(path,'..')

    ###retrieve the metallicity keys
    Zs= metallicity.get_keys()
    print Zs

    ###make necessary paths for output files
    if not os.path.exists(os.path.join(p,'output','%s'%name)):
        os.makedirs(os.path.join(p,'output','%s'%name))
    if not os.path.exists(os.path.join(p,'output','%s'%name,'hist')):
        os.makedirs(os.path.join(p,'output','%s'%name,'hist'))
    binp=os.path.join(p,'output','%s'%name)
    picklefile=os.path.join(binp,'%s_n%d.pkl'%(name,nsample))
    if VERBOSE: print "output files will be stored in ",binp
    if not CLOBBER and not NOPLOT:
        for key in Zs:
            for i in range(NM0,nm):
                checkhist(name,key,nsample,i,binp)
    if unpickle:
        RUNSIM=False
        if not os.path.isfile(picklefile):
            raw_input("missing pickled file for this simulation: name, nsample.\nrun the MonteCarlo? Ctr-C to exit, Return to continue?\n")
            RUNSIM=True
        else:
            pklfile = open(picklefile, 'rb')
            res=pickle.load(pklfile)

    if RUNSIM:
        ###Sample 'nsample' points from a gaussian centered on 0 with std 1
        mu=0
        sigma=1
        if nsample==1 : sample=[np.array([mu]) for i in range(NM0,nm)]

        else: sample=[np.random.normal(mu,sigma,newnsample) for i in range(NM0,nm)]

        ###Start calculation###
        ## the flux to be feed to the calculation will be
        ## flux + error*i
        ## where i is the sampled gaussian
        if VERBOSE: print "Starting iteration"

        #initialize the dictionary
        res={}
        for key in Zs:
            res[key]=[[] for i in range(NM0,nm)]

        #use only valid inputs
        delkeys=[]
        for k in bss[0].iterkeys():
            if k=='flag' or k=='galnum' or bss[0][k][1]==float('nan') :#or bss[1][k][1]==bss[0][k][1]:
                delkeys.append(k)
        for k in delkeys:
            del bss[0][k]
            del bss[1][k]


        import metscales as ms
        nps = min (mpc.cpu_count()-1 or 1, MAXPROCESSES)

        if multiproc and nps>1:
            print 'using multiproc!'
            scales=[ms.diagnostics(newnsample, logf,nps) for i in range(nm)]

            print >>logf, "\n\n\nrunning on %d threads\n\n\n"%nps
            second_args=[sample,flux,err,nm,bss,mds,VERBOSE, dust_corr,VERBOSE,res,scales,nps, logf]
            pool = mpc.Pool(processes=nps) # depends on available cores
            rr = pool.map(calc, itertools.izip(range(NM0,nm), itertools.repeat(second_args))) # for i in range(nm): result[i] = f(i, second_args)
            for ri,r  in enumerate(rr):
                for kk in r.iterkeys(): res[kk][ri]=r[kk][ri]

            for ri,r  in enumerate(rr):
                for kk in r.iterkeys(): res[kk][ri]=r[kk][ri]
            pool.close() # not optimal! but easy
            pool.join()
            for key in scales[i].mds.iterkeys():
                res[key]=np.array(res[key]).T
            if VERBOSE: print "Iteration Complete"
        else:
            #looping over nm spectra
            for i in range(NM0,nm):
                scales=ms.diagnostics(newnsample, logf,nps)
                print >>logf, "\n\n measurements ",i+1
                fluxi={}

                for k in bss[0].iterkeys():
                    print >>logf, '{0:15} '.format(k),
                    print >>logf, '{0:0.2} +/- {1:0.2}'.format(flux[k][i],err[k][i])
                    fluxi[k]=flux[k][i]*np.ones(len(sample[i]))+err[k][i]*sample[i]
                    warnings.filterwarnings("ignore")
                print >>logf,""

                success=metallicity.calculation(scales,fluxi,nm,mds,1,logf,disp=VERBOSE, dust_corr=dust_corr,verbose=VERBOSE)
                if success==-1:
                    print "MINIMUM REQUIRED LINES:  [OII]3727 & [OIII]5007, or [NII]6584, and Ha & Hb if you want dereddening"
                    #continue

                for key in scales.mds.iterkeys():
                    res[key][i]=scales.mds[key]
                    if res[key][i] is None:
                        res[key][i]=[float('NaN')]*newnsample
                    elif len(res[key][i])<newnsample:
                        res[key][i]=res[key][i]+[float('NaN')]*(newnsample-len(res[key][i]))

            for key in scales.mds.iterkeys():
                res[key]=np.array(res[key]).T
            if VERBOSE: print "Iteration Complete"

        #"WE CAN PICKLE THIS!"
        #pickle this realization
        if not NOPICKLE:
            pickle.dump(res,open(picklefile,'wb'))

    from matplotlib.font_manager import findfont, FontProperties

    if 'Time' not in  findfont(FontProperties()):
        fs=20
    if VERBOSE: print "FONT: %s, %d"%(findfont(FontProperties()),fs)


    ###Bin the results and save###
    print "\n\n"
    print '{0:15} {1:20} {2:>13}   -{3:>5}     +{4:>5}  {5:11} {6:>7}'.format("SN","diagnostic", "metallicity","34%", "34%", "(sample size:",'%d)'%nsample)
    for i in range(NM0,nm):
        if ASCIIOUTPUT:
            fi=open(os.path.join(binp,'%s_n%d_%d.txt'%(name,nsample,i+1)),'w')
            fi.write("%s\t Median Oxygen abundance (12+log(O/H))\t 16th percentile\t 84th percentile\n"%name)

        boxlabels=[]
        datas=[]
        print "\n\nmeasurement %d : %s-------------------------------------------------------------"%(i+1,flux[i]['galnum'])
        for key in Zs:
            if nsample==-1:
                try:
                    if ~np.isnan(res[key][i][0]):
                        print '{0:15} {1:20} {2:>13.3f}   -{3:>7.3f}   +{4:>7.3f} (no distribution)'.format(name+ ' %d'%(i+1),key,res[key][i][0],0,0 )
                except IndexError: pass
            else:
                try:
                    if sum(~np.isnan(res[key][:,i]))>0:
                        if ASCIIDISTRIB:
                            with open(os.path.join(binp,'%s_n%d_%s_%d.csv'%(name,nsample,key,i+1)), "wb") as fidist:
                                writer = csv.writer(fidist)
                                writer.writerow(res[key][:,i])
                        print
                        sh,data,kde=savehist(res[key][:,i],name,key,nsample,i,binp,nm,flux[:]['galnum'],verbose=verbose, fs=fs)
                        s=key+"\t "+sh+'\n'
                        if ASCIIOUTPUT:
                            fi.write(s)
                        if key not in ["E(B-V)" ,"logR23"]:
                            boxlabels.append(key.replace('_',' '))
                            datas.append(data)
                        if BINMODE == 'kd' and  not NOPICKLE:
                            pickleKDEfile=os.path.join(binp+'/%s_n%d_%s_%d_KDE.pkl'%(name,nsample,key,i+1))
                            if VERBOSE: print "KDE files will be stored in ",pickleKDEfile
                            pickle.dump(kde,open(pickleKDEfile,'wb'))
                except (IndexError,TypeError): pass
        #make box_and_whiskers plot
        fig= plt.figure(figsize=(8,15))
        fig.subplots_adjust(bottom=0.18,left=0.18)
        ax = fig.add_subplot(111)
        plt.grid()
        if len(datas)==0:
            continue

        bp = ax.boxplot(datas,patch_artist=True)
        for box in bp['boxes']:
            box.set( color='#7570b3', linewidth=2)
            box.set( facecolor = 'DarkOrange' , alpha=0.4)
        for whisker in bp['whiskers']:
            whisker.set(color='#7570b3', linewidth=2)
        for cap in bp['caps']:
            cap.set(color='#7570b3', linewidth=2)
        for median in bp['medians']:
            median.set(color='k', linewidth=2)
        for flier in bp['fliers']:
            flier.set(marker='o', color='#7570b3', alpha=0.4)
        plt.title("measurement %d: %s"%(i+1,flux[i]['galnum']))
        plt.xticks(range(1,len(boxlabels)+1), boxlabels, rotation=90, fontsize=fs-5)
        plt.fill_between(range(1,len(boxlabels)+1),[8.76]*len(boxlabels),[8.69]*len(boxlabels), facecolor='black', alpha=0.3)
        plt.text(1.2, 8.705,"Solar Oxygen Abundance", alpha=0.7)
        plt.gca().yaxis.set_major_formatter(FormatStrFormatter('%.2f'))
        plt.ylabel('12+log(O/H)', fontsize=fs)
        plt.savefig(binp+"/"+name+"_boxplot_n%d_%d.pdf"%(nsample,i+1),format='pdf')
        if ASCIIOUTPUT:
            fi.close()
        if VERBOSE: print "uncertainty calculation complete"
        #del datas

def get_key_md(md):
    key_md = {'D02': ['D02'], 'Z94': ['Z94'], 'M91': ['M91'], 'PP04': ['PP04'],
        'P10': ['P10'], 'M08': ['M08'], 'M13': ['M13'],
        'KD02': ['KD02_N2O2', 'KD_combined'],
        'KK04': ['KK04_N2Ha', 'KK04_R23']}

    return key_md[md]

class MCZ(object):
    def __init__(self, name, flux, err, **kwargs):
        '''
        flux and err should both be astropy tables
        '''

        for k, v in kwargs.iteritems():
            setattr(self, k, v)

        if not hasattr(self, 'NM0'):
            setattr(self, 'NM0', 0)
        if not hasattr(self, 'nps'):
            setattr(self, 'nps', None)
        if not hasattr(self, 'dust_corr'):
            setattr(self, 'dust_corr', True)
        if not hasattr(self, 'unpickle'):
            setattr(self, 'unpickle', False)
        if not hasattr(self, 'multiproc'):
            setattr(self, 'multiproc', False)
        if not hasattr(self, 'logf'):
            setattr(self, 'logf', 'logf.log')
        if not hasattr(self, 'verbose'):
            setattr(self, 'verbose', False)

        if self.unpickle == True:
            raise NotImplementedError('unpickling not yet supported')

        if self.multiproc == True:
            raise NotImplementedError('multi-processing not yet supported')

        self.nm = len(flux)

        self.flux = flux
        self.err = err
        self.name = name

    def estimate(self):
        # estimate metallicity using the emission-lines stated
        # in <obj>_meas.txt file

        return self.sample(1, True)

    def sample(self, nsample=1000, from_estimate=False):

        if (nsample == 1) and (from_estimate == False):
            raise ValueError(
                'for nsample = 1, use .estimate() method!')
        elif 1 < nsample < 100:
            raise ValueError(
                'need at least 100 samples!')

        # increasing sample by 10% to ensure
        # robustness against rejected samples
        nsample=int(nsample)

        # set up a dictionary to store tables of relevant data for each spaxel
        res_d = {}

        tfcnames = [k for k in self.flux.colnames
            if len(self.flux[k]) > np.isnan(self.flux[k]).sum()]
        self.tfcnames = tfcnames

        #looping over nm measurements
        for i in range(self.NM0, 3):#self.nm):
            galnum = self.flux['galnum'][i]
            fr = self.flux[i]
            er = self.err[i]

            fluxi = {k: np.random.normal(fr[k], er[k], nsample)
                    for k in fr.colnames
                    if ((k != 'galnum') and (~np.isnan(fr[k])))}

            # set up a table for a given galnum
            res_d[galnum] = t.Table()
            # add a column for flux information
            for n in tfcnames:
                if (n != 'galnum'):
                    if (np.isnan(self.flux[n]).sum() != len(self.flux[n])):
                        res_d[galnum][n] = fluxi[n]
                        res_d[galnum][n].unit = u.Unit('1e-17 erg cm^-2 s^-1')

            scales = ms.diagnostics(nsample, None, self.nps)

            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                success = metallicity.calculation(
                    scales, fluxi, self.nm, 'all', 1, self.logf,
                    disp=self.verbose, dust_corr=self.dust_corr,
                    verbose=self.verbose)
            if success==-1:
                raise ValueError('MINIMUM REQUIRED LINES:  [OII]3727 ' + \
                    '& [OIII] + 5007, or [NII]6584, and Ha & Hb if ' + \
                    'you want dereddening')

            for k, v in scales.mds.iteritems():
                if type(v) == np.ndarray:
                    if np.isnan(v).sum() != len(v):
                        res_d[galnum][k] = v

        self.res_d = res_d
        self.nsample = nsample

    def make_corner(self, galnum, scheme, lines=False):
        # make DFM-/emcee-style triangle plot

        res = self.res_d[galnum]

        colnames = res.colnames

        if self.nsample == 1:
            raise ValueError('need a sampled metallicity, not estimated! '+ \
                'Use self.sample(), with n fairly large')

        other_gcs = ['logR23', 'E(B-V)']
        if lines == True:
            other_gcs = ['[OII]3727', 'Hb', '[OIII]4959', '[OIII]5007',
                         '[OI]6300','Ha','[NII]6584','[SII]6717','[SII]6731',
                         '[SIII]9069','[SIII]9532'] + other_gcs

        if scheme == 'all':
            scheme = ['P10', 'M08', 'D02', 'M91', 'KD02', 'M13',
            'PP04', 'Z94', 'KK04', 'P10']
            raise RuntimeWarning('Caution using all Z indicators, ' + \
                'corner plots take a long time to generate')

            scheme_cols = [n for n in colnames if
                np.array(True if s in n else False for s in scheme).any()]

        elif scheme not in ['P10', 'M08', 'D02', 'M91', 'KD02', 'M13',
            'PP04', 'Z94', 'KK04', 'P10']:
            raise ValueError('bad metallicity scheme')

        else:
            scheme_cols = [n for n in colnames if scheme in n]

        good_cols = scheme_cols + other_gcs

        data = np.array([res[c] for c in good_cols]).T
        # get rid of pesky nans
        data = data[~np.isnan(data).any(axis=1)]

        labels = good_cols

        figure = corner.corner(
            data, labels=labels,
            quantiles=[0.16, 0.5, 0.84])

        # if we have multiple metallicity indicators, hist them all together
        if len(scheme_cols) > 1:
            scheme_colis = np.array(
                [True if l in scheme_cols else False
                for l in labels]).astype(bool)
            Ztot_ax = figure.add_axes([0.575, 0.675, 0.4, 0.3])
            Ztot_data = np.array([res[c] for c in scheme_cols]).flatten()
            Ztot_data = Ztot_data[~np.isnan(Ztot_data)]
            hist(
                Ztot_data, ax=Ztot_ax, bins='knuth', histtype='step',
                color='k')
            plt.xticks(rotation=45.)
            plt.setp(Ztot_ax.get_yticklabels(), visible=False)
            Ztot_ax.set_xlabel(r'$Z = 12 + \log{\frac{O}{H}}$')
            Ztot_ax.tick_params(axis='x', labelsize='x-small')

            pcs = np.percentile(Ztot_data, [14, 50, 86])
            for p in pcs:
                Ztot_ax.axvline(p, color='k', linestyle='--')

        figure.savefig('corner_{}_{}.png'.format(self.name, galnum))

    def write_fits(self, hdu):
        # make a fits header in the same style as the rest of the DAP outputs
        raise NotImplementedError('write_fits not implemented')



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('name', metavar='<name>', type=str, help="the SN file name (root of the _min,_max file names")
    parser.add_argument('nsample', metavar='N', type=int, help="number of iterations, minimum 100 (or 0 for no MC sampling)")
    parser.add_argument('--clobber',default=False, action='store_true', help="replace existing output")
    parser.add_argument('--binmode', default='k', type=str, choices=['d','s','k','t','bb','kd'], help='''method to determine bin size
    {d: Duanes formula, s: n^1/2, t: 2*n**1/3(default), k: Knuth's rule,
    bb: Bayesian blocks, kd: Kernel Density}''')
    parser.add_argument('--path',   default=None, type=str, help='''input/output path (must contain the input _max.txt and
    _min.txt files  in a subdirectory sn_data)''')
    parser.add_argument('--unpickle',   default=False, action='store_true', help="read the pickled realization instead of making a new one")

    parser.add_argument('--verbose',default=False, action='store_true', help="verbose mode")
    parser.add_argument('--log',default=None, type=str, help="log file, if not passed defaults to standard output")
    parser.add_argument('--nodust',default=False, action='store_true', help=" don't do dust corrections (default is to do it)")
    parser.add_argument('--noplot',default=False, action='store_true', help=" don't plot individual distributions (default is to plot all distributions)")
    parser.add_argument('--asciiout',default=False, action='store_true', help=" write distribution in an ascii output (default is not to)")
    parser.add_argument('--asciidistrib',default=False, action='store_true', help=" write distribution in an ascii output (default is not to)")
    parser.add_argument('--md',default='all', type =str, help= '''metallicity diagnostic to calculate.
    default is 'all', options are:
    D02, Z94, M91, C01, P05, M08, M08all, M13, PP04, D13, KD02, DP00 (deprecated), P01''')
    parser.add_argument('--multiproc',default=False, action='store_true', help=" multiprocess, with number of threads max(available cores-1, MAXPROCESSES)")
    args=parser.parse_args()

    global CLOBBER
    global VERBOSE
    global BINMODE
    global ASCIIOUTPUT
    global ASCIIDISTRIB
    global NOPLOT
    CLOBBER=args.clobber
    VERBOSE=args.verbose
    BINMODE=args.binmode
    NOPLOT=args.noplot
    ASCIIOUTPUT=args.asciiout
    ASCIIDISTRIB=args.asciidistrib

    if ASCIIDISTRIB:
        try: import csv
        except ImportError:
            raw_input("you must import the csv package to output the distribution. press return to continue")
            ASCIIDISTRIB=False

    if args.unpickle and NOPICKLE:
        args.unpickle = False
        raw_input("cannot use pickle on this machine, we won't save and won't read saved pickled realizations. Ctr-C to exit, Return to continue?\n")

    if args.path:
        path=args.path
    else:
        assert (os.getenv("MCMetdata")),''' the _max, _min (and _med) data must live in a folder named sn_data.
        pass a path to the sn_data folder, or set up the environmental variable
        MCMetdata pointing to the path where sn_data lives '''
        path=os.getenv("MCMetdata")
    assert(os.path.isdir(path)),"pass a path or set up the environmental variable MCMetdata pointing to the path where the _min _max _med files live"
    if args.nsample==1:
        print "CALCULATING METALLICITY WITHOUT GENERATING MC DISTRIBUTIONS"
    if args.nsample==1 or args.nsample>=100 :
        fi=input_data(args.name, path=path)
        for i, n in zip(
            fi, ['filename', 'meas', 'err', 'nm', 'path', 'bsmeas']):
            print n, ':', i, '\n-----+++++-----'
        if fi!=-1:
            logf=smart_open(args.log)
            run(fi,args.nsample,args.md,args.multiproc, logf, unpickle=args.unpickle, dust_corr=(not args.nodust), verbose=VERBOSE)
            if args.log: logf.close()
    else:
        print "nsample must be at least 100"


if __name__ == "__main__":
    if PROFILING:
        import cProfile
        cProfile.run("main()")
    else:
        main()


