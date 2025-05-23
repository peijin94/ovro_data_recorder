#!/usr/bin/env python3
    
import os
import sys
import glob
import h5py
import json
import time
import numpy
import queue
import ctypes
import signal
import logging
import argparse
import threading
from functools import reduce
from datetime import datetime, timedelta

from ovro_data_recorder.gridder import WProjection
from scipy.stats import scoreatpercentile as percentile

from lwa_antpos.station import ovro

from mnc.common import *
from mnc.mcs import ImageMonitorPoint, MultiMonitorPoint, Client

from ovro_data_recorder.reductions import *
from ovro_data_recorder.operations import FileOperationsQueue
from ovro_data_recorder.monitoring import GlobalLogger
from ovro_data_recorder.control import VisibilityCommandProcessor
from ovro_data_recorder.lwams import get_zenith_uvw
from ovro_data_recorder.version import version as odr_version
from ovro_data_recorder.paths import DATA as ODR_DATA_PATH, FONT as ODR_FONT_PATH

from ovro_data_recorder.xengine_fast_control import FastStation

from bifrost.address import Address
from bifrost.udp_socket import UDPSocket
from bifrost.packet_capture import PacketCaptureCallback, UDPCapture, DiskReader
from bifrost.ring import Ring
import bifrost.affinity as cpu_affinity
import bifrost.ndarray as BFArray
from bifrost.ndarray import copy_array
from bifrost.libbifrost import bf
from bifrost.proclog import ProcLog
from bifrost.memory import memcpy as BFMemCopy, memset as BFMemSet
from bifrost import asarray as BFAsArray

from casacore.tables import table as casa_table


import PIL.Image, PIL.ImageDraw, PIL.ImageFont


QUEUE = FileOperationsQueue()


def quota_size(value):
    """
    Convert a human readable time frame (e.g. 1d 4:00 for 1 day, 4 hours) into a
    number of seconds.
    """
    
    w = d = h = m = 0
    wfound = dfound = hfound = mfound = False
    try:
        w, value = value.split('w', 1)
        w = int(w)
        value = value.strip()
        wfound = True
    except (ValueError, TypeError):
        pass
    try:
        d, value = value.split('d', 1)
        d = int(d)
        value = value.strip()
        dfound = True
    except (ValueError, TypeError):
        pass
    try:
        h, value = value.split(':', 1)
        h = int(h)
        value = value.strip()
        hfound = True
    except (ValueError, TypeError):
        pass
    try:
        m = int(value)
        mfound = True
    except ValueError:
        pass
        
    if not (wfound or dfound or hfound or mfound):
        raise ValueError("Cannot interpret '%s' as a quota size" % value)
        
    value = 7*24*w + 24*d + h + m/60.0
    return int(value*3600)


FILL_QUEUE = queue.Queue(maxsize=1000)


def get_good_and_missing_rx():
    pid = os.getpid()
    statsname = os.path.join('/dev/shm/bifrost', str(pid), 'udp_capture', 'stats')
    
    good = 'ngood_bytes    : 0'
    missing = 'nmissing_bytes : 0'
    if os.path.exists(statsname):
        with open(os.path.join('/dev/shm/bifrost', str(pid), 'udp_capture', 'stats'), 'r'
) as fh:        
            good = fh.readline()
            missing = fh.readline()
    good = int(good.split(':', 1)[1], 10)
    missing = int(missing.split(':', 1)[1], 10)
    return good, missing


class CaptureOp(object):
    def __init__(self, log, sock, oring, nbl, ntime_gulp=1,
                 slot_ntime=6, fast=False, shutdown_event=None, core=None):
        self.log     = log
        self.sock    = sock
        self.oring   = oring
        self.nbl     = nbl
        self.ntime_gulp = ntime_gulp
        self.slot_ntime = slot_ntime
        self.fast    = fast
        if shutdown_event is None:
            shutdown_event = threading.Event()
        self.shutdown_event = shutdown_event
        self.core    = core
        
    def shutdown(self):
        self.shutdown_event.set()
        
    def seq_callback(self, seq0, time_tag, chan0, nchan, navg, nsrc, hdr_ptr, hdr_size_ptr):
        print("++++++++++++++++ seq0     =", seq0)
        print("                 time_tag =", time_tag)
        hdr = {'time_tag': time_tag,
               'seq0':     seq0, 
               'chan0':    chan0,
               'cfreq':    chan0*CHAN_BW,
               'nchan':    nchan,
               'bw':       nchan*CHAN_BW*(4 if self.fast else 1),
               'navg':     navg,
               'nstand':   int(numpy.sqrt(8*nsrc+1)-1)//2,
               'npol':     4,
               'nbl':      nsrc,
               'complex':  True,
               'nbit':     32}
        print("******** CFREQ:", hdr['cfreq'])
        hdr_str = json.dumps(hdr).encode()
        # TODO: Can't pad with NULL because returned as C-string
        #hdr_str = json.dumps(hdr).ljust(4096, '\0')
        #hdr_str = json.dumps(hdr).ljust(4096, ' ')
        header_buf = ctypes.create_string_buffer(hdr_str)
        hdr_ptr[0]      = ctypes.cast(header_buf, ctypes.c_void_p)
        hdr_size_ptr[0] = len(hdr_str)
        return 0
         
    def main(self):
        seq_callback = PacketCaptureCallback()
        seq_callback.set_cor(self.seq_callback)
        
        good, missing = get_good_and_missing_rx()
        with UDPCapture("cor", self.sock, self.oring, self.nbl, 1, 9000, 
                        self.ntime_gulp, self.slot_ntime,
                        sequence_callback=seq_callback, core=self.core) as capture:
            while not self.shutdown_event.is_set():
                status = capture.recv()
                
                # Determine the fill level of the last gulp
                new_good, new_missing = get_good_and_missing_rx()
                try:
                    fill_level = float(new_good-good) / (new_good-good + new_missing-missing)
                except ZeroDivisionError:
                    fill_level = 0.0
                good, missing = new_good, new_missing
                
                try:
                    FILL_QUEUE.put_nowait(fill_level)
                except queue.Full:
                    pass
                    
        del capture


class DummyOp(object):
    def __init__(self, log, sock, oring, nbl, ntime_gulp=1,
                 slot_ntime=6, fast=False, shutdown_event=None, core=None):
        self.log     = log
        self.sock    = sock
        self.oring   = oring
        self.nbl     = nbl
        self.ntime_gulp = ntime_gulp
        self.slot_ntime = slot_ntime
        self.fast    = fast
        if shutdown_event is None:
            shutdown_event = threading.Event()
        self.shutdown_event = shutdown_event
        self.core    = core
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        
        self.out_proclog.update( {'nring':1, 'ring0':self.oring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})
        
    def shutdown(self):
        self.shutdown_event.set()
          
    def main(self):
        with self.oring.begin_writing() as oring:
            navg  = 2400 if self.fast else 240000
            tint  = navg / CHAN_BW
            tgulp = tint * self.ntime_gulp
            nsrc  = self.nbl
            nbl   = self.nbl
            chan0 = 1234
            nchan = 192 // (4 if self.fast else 1)
            npol  = 4
            
            # Try to load model visibilities
            try:
                vis_base = numpy.load('utils/sky.npy')
            except:
                self.log.warn("Could not load model visibilities from utils/sky.py, using random data")
                vis_base = numpy.zeros((nbl, nchan, npol), dtype=numpy.complex64)
            assert(vis_base.shape[0] >= nbl)
            assert(vis_base.shape[1] >= nchan)
            assert(vis_base.shape[2] == npol)
            
            vis_base = vis_base[:self.nbl,::(4 if self.fast else 1),:]
            vis_base_r = (vis_base.real*1000).astype(numpy.int32)
            vis_base_i = (vis_base.imag*1000).astype(numpy.int32)
            vis_base = numpy.zeros((nbl, nchan, npol, 2), dtype=numpy.int32)
            vis_base[...,0] = vis_base_r
            vis_base[...,1] = vis_base_i
            
            ohdr = {'time_tag': int(int(time.time())*FS),
                    'seq0':     0, 
                    'chan0':    chan0,
                    'cfreq':    chan0*CHAN_BW,
                    'nchan':    nchan,
                    'bw':       nchan*CHAN_BW*(4 if self.fast else 1),
                    'navg':     navg*8192,
                    'nstand':   int(numpy.sqrt(8*nsrc+1)-1)//2,
                    'npol':     npol,
                    'nbl':      nbl,
                    'complex':  True,
                    'nbit':     32}
            ohdr_str = json.dumps(ohdr)
            
            ogulp_size = self.ntime_gulp*nbl*nchan*npol*8      # ci32
            oshape = (self.ntime_gulp,nbl,nchan,npol)
            self.oring.resize(ogulp_size)
            
            prev_time = time.time()
            with oring.begin_sequence(time_tag=ohdr['time_tag'], header=ohdr_str) as oseq:
                while not self.shutdown_event.is_set():
                    with oseq.reserve(ogulp_size) as ospan:
                        curr_time = time.time()
                        reserve_time = curr_time - prev_time
                        prev_time = curr_time
                        
                        odata = ospan.data_view(numpy.int32).reshape(oshape+(2,))
                        temp = vis_base + (1000*0.01*numpy.random.randn(*odata.shape)).astype(numpy.int32)
                        odata[...] = temp
                        
                        curr_time = time.time()
                        while curr_time - prev_time < tgulp:
                            time.sleep(0.01)
                            curr_time = time.time()
                            
                    curr_time = time.time()
                    process_time = curr_time - prev_time
                    prev_time = curr_time
                    self.perf_proclog.update({'acquire_time': -1, 
                                              'reserve_time': reserve_time, 
                                              'process_time': process_time,})


class SpectraOp(object):
    def __init__(self, log, id, iring, ntime_gulp=1, guarantee=True, core=-1):
        self.log        = log
        self.iring      = iring
        self.ntime_gulp = ntime_gulp
        self.guarantee  = guarantee
        self.core       = core
        
        self.client = Client(id)
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        
        self.in_proclog.update({'nring':1, 'ring0':self.iring.name})
        
    def _plot_spectra(self, time_tag, freq, specs):
        # Plotting setup
        nchan = freq.size
        nstand = specs.shape[0]
        try:
            minval = numpy.min(specs[numpy.where(numpy.isfinite(specs))])
            maxval = numpy.max(specs[numpy.where(numpy.isfinite(specs))])
        except ValueError:
            minval = 0.0
            maxval = 1.0
            
        # Image setup
        width = 20
        height = 18
        im = PIL.Image.new('RGB', (width * 65 + 1, height * 65 + 21), '#FFFFFF')
        draw = PIL.ImageDraw.Draw(im)
        font = PIL.ImageFont.load(os.path.join(ODR_FONT_PATH, 'helvB10.pil'))
       
        # Axes boxes
        for i in range(width + 1):
            draw.line([i * 65, 0, i * 65, height * 65], fill = '#000000')
        for i in range(height + 1):
            draw.line([(0, i * 65), (im.size[0], i * 65)], fill = '#000000')
            
        # Power as a function of frequency for all antennas
        x = numpy.arange(nchan) * 64 // nchan
        for s in range(nstand):
            if s >= height * width:
                break
            x0, y0 = (s % width) * 65 + 1, (s // width + 1) * 65
            draw.text((x0 + 5, y0 - 60), str(s+1), font=font, fill='#000000')
            
            ## XX
            c = '#1F77B4'
            y = ((54.0 / (maxval - minval)) * (specs[s,:,0] - minval)).clip(0, 54)
            draw.point(list(zip(x0 + x, y0 - y)), fill=c)
            
            ## YY
            c = '#FF7F0E'
            y = ((54.0 / (maxval - minval)) * (specs[s,:,1] - minval)).clip(0, 54)
            draw.point(list(zip(x0 + x, y0 - y)), fill=c)
            
        # Summary
        ySummary = height * 65 + 2
        timeStr = datetime.utcfromtimestamp(time_tag / FS)
        timeStr = timeStr.strftime("%Y/%m/%d %H:%M:%S UTC")
        draw.text((5, ySummary), timeStr, font = font, fill = '#000000')
        rangeStr = 'range shown: %.3f to %.3f dB' % (minval, maxval)
        draw.text((210, ySummary), rangeStr, font = font, fill = '#000000')
        x = im.size[0] + 15
        for label, c in reversed(list(zip(('XX',     'YY'),
                                          ('#1F77B4','#FF7F0E')))):
            x -= draw.textsize(label, font = font)[0] + 20
            draw.text((x, ySummary), label, font = font, fill = c)
            
        return im
        
    def main(self):
        cpu_affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1, 
                                  'core0': cpu_affinity.get_core(),})
        
        for iseq in self.iring.read(guarantee=self.guarantee):
            ihdr = json.loads(iseq.header.tostring())
            
            self.sequence_proclog.update(ihdr)
            
            self.log.info("Spectra: Start of new sequence: %s", str(ihdr))
            
            # Setup the ring metadata and gulp sizes
            time_tag = ihdr['time_tag']
            navg     = ihdr['navg']
            nbl      = ihdr['nbl']
            nstand   = ihdr['nstand']
            chan0    = ihdr['chan0']
            nchan    = ihdr['nchan']
            chan_bw  = ihdr['bw'] / nchan
            npol     = ihdr['npol']
            
            igulp_size = self.ntime_gulp*nbl*nchan*npol*8   # ci32
            ishape = (self.ntime_gulp,nbl,nchan,npol)
            
            # Setup the arrays for the frequencies and auto-correlations
            freq = chan0*chan_bw + numpy.arange(nchan)*chan_bw
            autos = [i*(2*(nstand-1)+1-i)//2 + i for i in range(nstand)]
            last_save = 0.0
            
            prev_time = time.time()
            for ispan in iseq.read(igulp_size):
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time
                
                ## Setup and load
                idata = ispan.data_view(numpy.int32).reshape(ishape+(2,))
                
                if time.time() - last_save > 60:
                    ## Timestamp
                    tt = LWATime(time_tag, format='timetag')
                    ts = tt.unix
                    
                    ## Pull out the auto-correlations
                    adata = idata[0,autos,:,:,0]
                    adata = adata[:,:,[0,3]]
                    
                    ## Plot
                    im = self._plot_spectra(time_tag, freq, 10*numpy.log10(adata))
                    
                    ## Save
                    mp = ImageMonitorPoint.from_image(im)
                    self.client.write_monitor_point('diagnostics/spectra',
                                                    mp, timestamp=ts)
                    del mp
                    del im
                    
                    last_save = time.time()
                    
                time_tag += navg * self.ntime_gulp
                
                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time, 
                                          'reserve_time': 0.0, 
                                          'process_time': process_time,})
                
        self.log.info("SpectraOp - Done")


class BaselineOp(object):
    def __init__(self, log, id, station, iring, ntime_gulp=1, guarantee=True, core=-1):
        self.log        = log
        self.station    = station
        self.iring      = iring
        self.ntime_gulp = ntime_gulp
        self.guarantee  = guarantee
        self.core       = core
        
        self.client = Client(id)
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        
        self.in_proclog.update({'nring':1, 'ring0':self.iring.name})
        
    def _plot_baselines(self, time_tag, freq, dist, baselines, valid):
        # Plotting setup
        nchan = freq.size
        nbl = baselines.shape[0]
        freq = freq[nchan//2]
        baselines = baselines[valid,nchan//2,:]
        baselines = numpy.abs(baselines[:,[0,1,3]])
        minval = numpy.min(baselines)
        maxval = numpy.max(baselines)
        if minval == maxval:
            maxval = minval + 1.0
            
        mindst = 0.0
        maxdst = numpy.max(dist)
        
        # Image setup
        im = PIL.Image.new('RGB', (601, 421), '#FFFFFF')
        draw = PIL.ImageDraw.Draw(im)
        font = PIL.ImageFont.load(os.path.join(ODR_FONT_PATH, 'helvB10.pil'))
        
        # Axes boxes
        for i in range(2):
            draw.line([i * 600, 0, i * 600, 400], fill = '#000000')
        for i in range(2):
            draw.line([(0, i * 400), (im.size[0], i * 400)], fill = '#000000')
            
        # Visiblity amplitudes as a function of (u,v) distance
        x0, y0 = 1, 400
        draw.text((x0 + 500, y0 - 395), '%.3f MHz' % (freq/1e6,), font=font, fill='#000000')
        
        ## (u,v) distance
        x = ((599.0 / (maxdst - mindst)) * (dist - mindst)).clip(0, 599)
        
        ## XX
        y = ((399.0 / (maxval - minval)) * (baselines[:,0] - minval)).clip(0, 399)
        draw.point(list(zip(x0 + x, y0 - y)), fill='#1F77B4')
        
        ## YY
        y = ((399.0 / (maxval - minval)) * (baselines[:,2] - minval)).clip(0, 399)
        draw.point(list(zip(x0 + x, y0 - y)), fill='#FF7F0E')
        
        ### XY
        #y = ((399.0 / (maxval - minval)) * (baselines[:,1] - minval)).clip(0, 399)
        #draw.point(list(zip(x0 + x, y0 - y)), fill='#A00000')
        
        # Details and labels
        ySummary = 402
        timeStr = datetime.utcfromtimestamp(time_tag / FS)
        timeStr = timeStr.strftime("%Y/%m/%d %H:%M:%S UTC")
        draw.text((5, ySummary), timeStr, font = font, fill = '#000000')
        rangeStr = 'range shown: %.6f - %.6f' % (minval, maxval)
        draw.text((210, ySummary), rangeStr, font = font, fill = '#000000')
        x = im.size[0] + 15
        #for label, c in reversed(list(zip(('XX','XY','YY'), ('#1F77B4','#A00000','#FF7F0E')))):
        for label, c in reversed(list(zip(('XX','YY'), ('#1F77B4','#FF7F0E')))):
            x -= draw.textsize(label, font = font)[0] + 20
            draw.text((x, ySummary), label, font = font, fill = c)
            
        return im
        
    def main(self):
        cpu_affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1, 
                                  'core0': cpu_affinity.get_core(),})
        
        for iseq in self.iring.read(guarantee=self.guarantee):
            ihdr = json.loads(iseq.header.tostring())
            
            self.sequence_proclog.update(ihdr)
            
            self.log.info("Baseline: Start of new sequence: %s", str(ihdr))
            
            # Setup the ring metadata and gulp sizes
            time_tag = ihdr['time_tag']
            navg     = ihdr['navg']
            nbl      = ihdr['nbl']
            nstand   = ihdr['nstand']
            chan0    = ihdr['chan0']
            nchan    = ihdr['nchan']
            chan_bw  = ihdr['bw'] / nchan
            npol     = ihdr['npol']
            
            igulp_size = self.ntime_gulp*nbl*nchan*npol*8
            ishape = (self.ntime_gulp,nbl,nchan,npol)
            self.iring.resize(igulp_size)
            
            # Setup the arrays for the frequencies and baseline lenghts
            freq = chan0*chan_bw + numpy.arange(nchan)*chan_bw
            uvw = get_zenith_uvw(self.station, LWATime(time_tag, format='timetag'))
            uvw[:,2] = 0
            dist = numpy.sqrt((uvw**2).sum(axis=1))
            valid = numpy.where(dist > 0.1)[0]
            last_save = 0.0
            
            prev_time = time.time()
            for ispan in iseq.read(igulp_size):
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time
                
                ## Setup and load
                idata = ispan.data_view(numpy.int32).reshape(ishape+(2,))
                
                if time.time() - last_save > 60:
                    ## Timestamp
                    tt = LWATime(time_tag, format='timetag')
                    ts = tt.unix
                    
                    ## Plot
                    try:
                        bdata.real[...] = idata[0,:,:,:,0]
                        bdata.imag[...] = idata[0,:,:,:,1]
                    except NameError:
                        bdata = idata[0,:,:,:,0] + 1j*idata[0,:,:,:,1]
                        bdata = bdata.astype(numpy.complex64)
                    im = self._plot_baselines(time_tag, freq, dist, bdata, valid)
                    
                    ## Save
                    mp = ImageMonitorPoint.from_image(im)
                    self.client.write_monitor_point('diagnostics/baselines',
                                                    mp, timestamp=ts)
                    del mp
                    del im
                    
                    last_save = time.time()
                    
                time_tag += navg * self.ntime_gulp
                
                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time, 
                                          'reserve_time': 0.0, 
                                          'process_time': process_time,})
                
            try:
                del bdata
            except NameError:
                pass
                
        self.log.info("BaselineOp - Done")


class ImageOp(object):
    def __init__(self, log, id, station, iring, cal_dir=None, ntime_gulp=1, guarantee=True, core=-1):
        self.log        = log
        self.station    = station
        self.iring      = iring
        self.cal_dir    = cal_dir
        self.ntime_gulp = ntime_gulp
        self.guarantee  = guarantee
        self.core       = core
        
        self.client = Client(id)
        self._caltag = -1
        self._last_cal_update = 0.0
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        
        self.in_proclog.update({'nring':1, 'ring0':self.iring.name})
        
    def _load_calibration(self, nstand, nbl, freq):
        cal = None
        
        if self.cal_dir is not None:
            # Get the modification time of the calibration directory
            last_update = os.path.getmtime(self.cal_dir)
            if last_update > self._last_cal_update:
                ## Looks like the directory has been updated, reload
                self.log.info("Image: Reloading calibration tables from '%s'", self.cal_dir)
                calfiles = glob.glob(os.path.join(self.cal_dir, '*.bcal'))
                
                ## Load all calibration tables and save them be the first frequency
                ## in each rouned to the nearest Hz
                self._all_cals = {}
                for calfile in calfiles:
                    ### Calibration and flagging data
                    caltab = casa_table(calfile, ack=False)
                    calant = caltab.getcol('ANTENNA1')[...]
                    caldata = caltab.getcol('CPARAM')[:,:,:]
                    calflag = caltab.getcol('FLAG')[:,:,:]
                    caltab.close()
                    
                    ### Calibration frequency range
                    caltab = casa_table(os.path.join(calfile, 'SPECTRAL_WINDOW'), ack=False)
                    calfreq = caltab.getcol('CHAN_FREQ')[...]
                    calfreq = calfreq.ravel()
                    caltab.close()
                    
                    ### Cache
                    caltag = int(round(calfreq[0]))
                    self._all_cals[caltag] = {'freq': calfreq,
                                              'ant':  calant,
                                              'data': caldata,
                                              'flag': calflag}
                ## Remove the old cached information and update the update time
                try:
                    del self._cal
                except AttributeError:
                    pass
                self._caltag = -1
                self._last_cal_update = last_update
                
            # Get the "calibration tag" for the current data set
            caltag = int(round(freq[0]))
            
            if caltag == self._caltag:
                # Great, we already have it
                cal = self._cal
            else:
                # We need to make a new one for each baseline/channel/polarization
                # NOTE: Lots of assumptions here about the antenna order
                self._cal = numpy.zeros((2*nbl,freq.size,4), dtype=numpy.complex64)
                base_cal = self._all_cals[caltag]
                k = 0
                for i in range(nstand):
                    gix = (1 - base_cal['flag'][i,:,0]) / base_cal['data'][i,:,0]
                    giy = (1 - base_cal['flag'][i,:,1]) / base_cal['data'][i,:,1]
                    gix[numpy.where(~numpy.isfinite(gix))] = 0.0
                    giy[numpy.where(~numpy.isfinite(giy))] = 0.0
                    
                    for j in range(i,nstand):
                        gjx = (1 - base_cal['flag'][j,:,0]) / base_cal['data'][j,:,0]
                        gjy = (1 - base_cal['flag'][j,:,1]) / base_cal['data'][j,:,1]
                        gjx[numpy.where(~numpy.isfinite(gjx))] = 0.0
                        gjy[numpy.where(~numpy.isfinite(gjy))] = 0.0
                        
                        self._cal[k,:,0] = gix*gjx.conj()
                        self._cal[k,:,1] = gix*gjy.conj()
                        self._cal[k,:,2] = giy*gjx.conj()
                        self._cal[k,:,3] = giy*gjy.conj()
                        self._cal[nbl+k,:,:] = self._cal[k,:,:].conj()
                        k += 1
                        
                # Update cal and the "calibration tag"
                cal = self._cal
                self._caltag = caltag
                
        # Done - this can be None
        return cal
        
    @staticmethod
    def _colormap_and_convert(array, limits=[5, 99.95]):
        output = numpy.zeros(array.shape+(3,), dtype=numpy.uint8)
        
        vmin, vmax = percentile(array.ravel(), limits)
        if vmax == vmin:
            vmax = vmin + 1
        array -= vmin
        array /= (vmax-vmin)
        output[...,0] = numpy.clip((-7.55*array**2 + 11.06*array - 2.96)*255, 0, 255)
        output[...,1] = numpy.clip((-7.33*array**2 +  7.57*array - 0.83)*255, 0, 255)
        output[...,2] = numpy.clip((-7.55*array**2 +  4.04*array + 0.55)*255, 0, 255)
        return PIL.Image.fromarray(output).convert('RGB')
    
    def _plot_images(self, time_tag, freq, uvw, baselines, valid, order, has_cal=False):
        # Plotting setup
        nchan = freq.size
        nbl = baselines.shape[0]
        freq = freq[:4]
        uvw = uvw[valid,:,:4]
        baselines = baselines[valid,:4,:]
        wgts = numpy.ones(baselines.shape, dtype=numpy.float32)
        
        # Form I and V visibilities
        baselinesI = baselines[...,0] + baselines[...,3]
        baselinesV = baselines[...,1] - baselines[...,2]
        baselinesV[baselinesV.shape[0]//2:,:] *= 1j
        temp = baselinesV.imag
        baselinesV.imag = baselinesV.real
        baselinesV.real = -temp

        # Image I and V
        imageI, _, corr = WProjection(uvw[order,0,:].ravel(), uvw[order,1,:].ravel(), uvw[order,2,:].ravel(),
                                       baselinesI[order,:].ravel(), wgts[order,:,0].ravel(),
                                       200, 0.5, 0.1)
        imageV, _, corr = WProjection(uvw[order,0,:].ravel(), uvw[order,1,:].ravel(), uvw[order,2,:].ravel(),
                                       baselinesV[order,:].ravel(), wgts[order,:,3].ravel(),
                                       200, 0.5, 0.1)
        imageI = numpy.fft.fftshift(numpy.fft.ifft2(imageI).real / corr)
        imageV = numpy.fft.fftshift(numpy.fft.ifft2(imageV).real / corr)
        
        # Map the color scale
        imI = self._colormap_and_convert(imageI[::-1,:])
        imV = self._colormap_and_convert(numpy.abs(imageV[::-1,:]))
        
        # Image setup
        im = PIL.Image.new('RGB', (860, 420))
        draw = PIL.ImageDraw.Draw(im)
        font = PIL.ImageFont.load(os.path.join(ODR_FONT_PATH, 'helvB10.pil'))
        
        ## I
        im.paste(imI, ( 20, 20))

        ## |V|
        im.paste(imV, (440, 20))

        ## Horizon circles + outside horizon blanking
        draw.ellipse(( 20, 20,419,419), fill=None, outline='#000000')
        draw.ellipse((440, 20,839,419), fill=None, outline='#000000')
        for i in range(4):
            ip = 20 + 399*(i//2)
            jp = 20 + 399*(i%2)
            PIL.ImageDraw.floodfill(im, (ip,    jp), value=(0,0,0), border=(0,0,0))
            PIL.ImageDraw.floodfill(im, (ip+420,jp), value=(0,0,0), border=(0,0,0))
            
        # Details and labels
        timeStr = datetime.utcfromtimestamp(time_tag / FS)
        timeStr = timeStr.strftime("%Y/%m/%d %H:%M:%S UTC")
        calStr = 'Uncal'
        if has_cal:
            calStr = 'Cal'
        draw.text((  5,  5), timeStr, font = font, fill = '#FFFFFF')
        draw.text((785,  5), "%.2f MHz" % (freq.mean()/1e6,), font = font, fill = '#FFFFFF')
        draw.text((805,405), calStr, font = font, fill = '#FFFFFF')
        draw.text((  5, 30), 'I', font = font, fill = '#FFFFFF')
        draw.text((835, 30), '|V|', font = font, fill = '#FFFFFF')
        
        ## Logo-ize
        logo = PIL.Image.open(os.path.join(ODR_DATA_PATH, 'logo.png'))
        logo_img = logo.getchannel('A')
        im.paste(logo_img, (5, 385))
        logo.close()
        
        return im
        
    def main(self):
        cpu_affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1, 
                                  'core0': cpu_affinity.get_core(),})
        
        for iseq in self.iring.read(guarantee=self.guarantee):
            ihdr = json.loads(iseq.header.tostring())
            
            self.sequence_proclog.update(ihdr)
            
            self.log.info("Image: Start of new sequence: %s", str(ihdr))
            
            # Setup the ring metadata and gulp sizes
            time_tag = ihdr['time_tag']
            navg     = ihdr['navg']
            nbl      = ihdr['nbl']
            nstand   = ihdr['nstand']
            chan0    = ihdr['chan0']
            nchan    = ihdr['nchan']
            chan_bw  = ihdr['bw'] / nchan
            npol     = ihdr['npol']
            
            igulp_size = self.ntime_gulp*nbl*nchan*npol*8
            ishape = (self.ntime_gulp,nbl,nchan,npol)
            self.iring.resize(igulp_size, 10*igulp_size)
            
            # Setup the arrays for the frequencies and baseline lenghts
            freq = chan0*chan_bw + numpy.arange(nchan)*chan_bw
            uvw = get_zenith_uvw(self.station, LWATime(time_tag, format='timetag'))
            uvw = numpy.concatenate([uvw, -uvw], axis=0)
            dist = numpy.sqrt((uvw[:,:2]**2).sum(axis=1))
            uscl = freq / 299792458.0
            uscl.shape = (1,1)+uscl.shape
            uvw.shape += (1,)
            uvw = uvw*uscl
            valid = numpy.where((dist > 0.1) & (dist < 250))[0]
            order = numpy.argsort(uvw[valid,2,0])
            last_save = 0.0
            
            prev_time = time.time()
            for ispan in iseq.read(igulp_size):
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time
                
                ## Setup and load
                idata = ispan.data_view(numpy.int32).reshape(ishape+(2,))
                
                if time.time() - last_save > 60:
                    t0 = time.time()
                    ## Timestamp
                    tt = LWATime(time_tag, format='timetag')
                    ts = tt.unix
                    
                    ## Load the calibration
                    try:
                        cal = self._load_calibration(nstand, nbl, freq)
                    except Exception as e:
                        self.log.warn("Image: Failed to load calibration solutions: %s", str(e))
                        cal = None
                        
                    ## Plot
                    try:
                        bdata.real[:nbl,...] =  idata[0,:,:,:,0]
                        bdata.imag[:nbl,...] =  idata[0,:,:,:,1]
                        bdata.real[nbl:,...] =  idata[0,:,:,:,0]
                        bdata.imag[nbl:,...] = -idata[0,:,:,:,1]
                    except NameError:
                        bdata = idata[0,:,:,:,0] + 1j*idata[0,:,:,:,1]
                        bdata = bdata.astype(numpy.complex64)
                        bdata = numpy.concatenate([bdata, bdata.conj()], axis=0)
                    if cal is not None:
                        bdata *= cal
                    im = self._plot_images(time_tag, freq, uvw, bdata, valid, order, has_cal=(cal is not None))
                    
                    ## Save
                    mp = ImageMonitorPoint.from_image(im)
                    self.client.write_monitor_point('diagnostics/image',
                                                    mp, timestamp=ts)
                    del mp
                    del im
                    
                    last_save = time.time()
                    
                time_tag += navg * self.ntime_gulp
                
                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time, 
                                          'reserve_time': 0.0, 
                                          'process_time': process_time,})
                
            try:
                del bdata
            except NameError:
                pass
                
        self.log.info("ImageOp - Done")


class StatisticsOp(object):
    def __init__(self, log, id, iring, ntime_gulp=1, guarantee=True, core=None):
        self.log        = log
        self.iring      = iring
        self.ntime_gulp = ntime_gulp
        self.guarantee  = guarantee
        self.core       = core
        
        self.client = Client(id)
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        
        self.in_proclog.update(  {'nring':1, 'ring0':self.iring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})
        
    def main(self):
        if self.core is not None:
            cpu_affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1, 
                                  'core0': cpu_affinity.get_core(),})
        
        for iseq in self.iring.read(guarantee=self.guarantee):
            ihdr = json.loads(iseq.header.tostring())
            
            self.sequence_proclog.update(ihdr)
            
            self.log.info("Statistics: Start of new sequence: %s", str(ihdr))
            
            # Setup the ring metadata and gulp sizes
            time_tag = ihdr['time_tag']
            navg     = ihdr['navg']
            nbl      = ihdr['nbl']
            nstand   = ihdr['nstand']
            chan0    = ihdr['chan0']
            nchan    = ihdr['nchan']
            chan_bw  = ihdr['bw'] / nchan
            npol     = ihdr['npol']
            
            igulp_size = self.ntime_gulp*nbl*nchan*npol*8        # ci32
            ishape = (self.ntime_gulp,nbl,nchan,npol)
            
            autos = [i*(2*(nstand-1)+1-i)//2 + i for i in range(nstand)]
            data_pols = ['XX', 'YY']
            last_save = 0.0
            
            prev_time = time.time()
            iseq_spans = iseq.read(igulp_size)
            for ispan in iseq_spans:
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time
                
                ## Setup and load
                idata = ispan.data_view('ci32').reshape(ishape)
                
                if time.time() - last_save > 60:
                    ## Timestamp
                    tt = LWATime(time_tag, format='timetag')
                    ts = tt.unix
                    
                    ## Pull out the auto-correlations
                    idata = idata.view(numpy.int32).reshape(ishape+(2,))
                    adata = idata[0,autos,:,:,0]
                    adata = adata[:,:,[0,3]]
                    
                    ## Run the statistics over all times/channels
                    ##  * only really works for ntime_gulp=1
                    data_min = numpy.min(adata, axis=1)
                    data_max = numpy.max(adata, axis=1)
                    data_avg = numpy.mean(adata, axis=1)
                    
                    ## Save
                    for data,name in zip((data_min,data_avg,data_max), ('min','avg','max')):
                        value = MultiMonitorPoint([data[:,i].tolist() for i in range(data.shape[1])],
                                                  timestamp=ts, field=data_pols)
                        self.client.write_monitor_point('statistics/%s' % name, value)
                        del value
                        
                    last_save = time.time()
                    
                time_tag += navg * self.ntime_gulp
                
                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time, 
                                          'reserve_time': -1, 
                                          'process_time': process_time,})
                
        self.log.info("StatisticsOp - Done")


class WriterOp(object):
    def __init__(self, log, mcs_id, station, iring, ntime_gulp=1, fast=False, guarantee=True, core=None):
        self.log        = log
        self.station    = station
        self.iring      = iring
        self.ntime_gulp = ntime_gulp
        self.fast       = fast
        self.guarantee  = guarantee
        self.core       = core
        self.client     = Client(mcs_id)
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        self.err_proclog = ProcLog(type(self).__name__+"/error")
        
        self.in_proclog.update(  {'nring':1, 'ring0':self.iring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})
        self.err_proclog.update( {'nerror':0, 'last': ''})
        
    def main(self):
        global QUEUE
        global FILL_QUEUE
        
        if self.core is not None:
            cpu_affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1, 
                                  'core0': cpu_affinity.get_core(),})
        
        was_active = False
        for iseq in self.iring.read(guarantee=self.guarantee):
            ihdr = json.loads(iseq.header.tostring())
            
            self.sequence_proclog.update(ihdr)
            
            self.log.info("Writer: Start of new sequence: %s", str(ihdr))
            
            # Setup the ring metadata and gulp sizes
            time_tag = ihdr['time_tag']
            navg     = ihdr['navg']
            nbl      = ihdr['nbl']
            chan0    = ihdr['chan0']
            nchan    = ihdr['nchan']
            chan_bw  = ihdr['bw'] / nchan
            npol     = ihdr['npol']
            pols     = ['XX','XY','YX','YY']
            
            igulp_size = self.ntime_gulp*nbl*nchan*npol*8        # ci32
            ishape = (self.ntime_gulp,nbl,nchan,npol)
            self.iring.resize(igulp_size, 10*igulp_size*(4 if self.fast else 1))
            
            norm_factor = navg // (2*NCHAN) * (4 if self.fast else 1)
            
            self.client.write_monitor_point('latest_frequency', chan_to_freq(chan0), unit='Hz')
            
            first_gulp = True
            write_error_asserted = False
            write_error_counter = 0
            prev_time = time.time()
            iseq_spans = iseq.read(igulp_size)
            for ispan in iseq_spans:
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time
                
                ## On our first span, update the pipeline lag for the queue
                ## so that we start recording at the right times
                if first_gulp:
                    QUEUE.update_lag(LWATime(time_tag, format='timetag').datetime)
                    self.log.info("Current pipeline lag is %s", QUEUE.lag)
                    first_gulp = False
                    
                ## Setup and load
                idata = ispan.data_view(numpy.int32).reshape(ishape+(2,))
                try:
                    cdata.real[...] = idata[...,0]
                    cdata.imag[...] = idata[...,1]
                except NameError:
                    cdata = idata[...,0] + 1j*idata[...,1]
                    cdata = cdata.astype(numpy.complex64)
                cdata /= norm_factor
                
                ## Poll the fill level
                try:
                    fill_level = FILL_QUEUE.get_nowait()
                    FILL_QUEUE.task_done()
                except queue.Empty:
                    self.log.warn("Failed to get integration fill level")
                    fill_level = -1.0
                    
                ## Determine what to do
                active_op = QUEUE.active
                if active_op is not None:
                    ### Recording active - write
                    if not active_op.is_started:
                        self.log.info("Started operation - %s", active_op)
                        active_op.start(self.station, chan0, navg, nchan, chan_bw, npol, pols)
                        was_active = True
                    try:
                        active_op.write(time_tag, cdata, fill_level=fill_level)
                        if not self.fast:
                            self.client.write_monitor_point('latest_time_tag', time_tag)
                            
                        if write_error_asserted:
                            write_error_asserted = False
                            self.log.info("Write error de-asserted - count was %i", write_error_counter)
                            self.err_proclog.update({'nerror':0, 'last': ''})
                            write_error_counter = 0
                            
                    except Exception as e:
                        if not write_error_asserted:
                            write_error_asserted = True
                            self.log.error("Write error asserted - initial error: %s", str(e))
                            self.err_proclog.update({'nerror':1, 'last': str(e).replace(':','--')})
                        write_error_counter += 1
                        
                        if write_error_counter % 50 == 0:
                            self.log.error("Write error re-asserted - count is %i - latest error: %s", write_error_counter, str(e))
                            self.err_proclog.update( {'nerror':write_error_counter, 'last': str(e).replace(':','--')})
                            
                elif was_active:
                    ### Recording just finished
                    #### Clean
                    was_active = False
                    QUEUE.clean()
                    
                    #### Close
                    self.log.info("Ended operation - %s", QUEUE.previous)
                    QUEUE.previous.stop()
                time_tag += navg
                
                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time, 
                                          'reserve_time': -1, 
                                          'process_time': process_time,})
                
            try:
                del cdata
            except NameError:
                pass
                
        self.client.write_monitor_point('latest_frequency', None, unit='Hz')
        
        self.log.info("WriterOp - Done")


def main(argv):
    global QUEUE
    
    parser = argparse.ArgumentParser(
                 description="Data recorder for slow/fast visibility data"
                 )
    parser.add_argument('-a', '--address', type=str, default='127.0.0.1',
                        help='IP address to listen to')
    parser.add_argument('-p', '--port', type=int, default=10000,
                        help='UDP port to receive data on')
    parser.add_argument('-o', '--offline', action='store_true',
                        help='run in offline using the specified file to read from')
    parser.add_argument('-c', '--cores', type=str, default='0,1,2,3,4,5',
                        help='comma separated list of cores to bind to')
    parser.add_argument('-g', '--gulp-size', type=int, default=1,
                        help='gulp size for ring buffers')
    parser.add_argument('-l', '--logfile', type=str,
                        help='file to write logging to')
    parser.add_argument('--debug', action='store_true',
                        help='enable debugging messages in the log')
    parser.add_argument('-r', '--record-directory', type=str, default=os.path.abspath('.'),
                        help='directory to save recorded files to')
    parser.add_argument('-t', '--record-directory-quota', type=quota_size, default=0,
                        help='quota for the recording directory, 0 disables the quota')
    parser.add_argument('-q', '--quick', action='store_true',
                        help='run in fast visibiltiy mode')
    parser.add_argument('-i', '--nint-per-file', type=int, default=1,
                        help='number of integrations to write per measurement set')
    parser.add_argument('-n', '--no-tar', action='store_true',
                        help='do not store the measurement sets inside a tar file')
    parser.add_argument('-f', '--fork', action='store_true',
                        help='fork and run in the background')
    parser.add_argument('--image', action='store_true',
                        help='generate images for the inner core and a subset of the bandwidth')
    parser.add_argument('--cal-dir', type=str,
                        help='directory to look for beamformer calibration data in (only for --image)')
    args = parser.parse_args()
    
    # Process the -q/--quick option
    station = ovro
    if args.quick:
        args.nint_per_file = max([10, args.nint_per_file])
        station = FastStation(servers=['lxdlwagpu01'], station=ovro)
        
    # Fork, if requested
    if args.fork:
        stderr = '/tmp/%s_%i.stderr' % (os.path.splitext(os.path.basename(__file__))[0], args.port)
        daemonize(stdin='/dev/null', stdout='/dev/null', stderr=stderr)
        
    # Setup logging
    log = logging.getLogger(__name__)
    logFormat = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    logFormat.converter = time.gmtime
    if args.logfile is None:
        logHandler = logging.StreamHandler(sys.stdout)
    else:
        logHandler = LogFileHandler(args.logfile)
    logHandler.setFormatter(logFormat)
    log.addHandler(logHandler)
    log.setLevel(logging.DEBUG if args.debug else logging.INFO)
    
    log.info("Starting %s with PID %i", os.path.basename(__file__), os.getpid())
    log.info("Version: %s", odr_version)
    log.info("Cmdline args:")
    for arg in vars(args):
        log.info("  %s: %s", arg, getattr(args, arg))
        
    # Setup the subsystem ID
    mcs_id = 'drv'
    if args.quick:
        mcs_id += 'f'
    else:
        mcs_id += 's'
    base_ip = int(args.address.split('.')[-1], 10)
    base_port = args.port % 100
    mcs_id += str(base_ip*100 + base_port)
    
    # Setup the cores and GPUs to use
    cores = [int(v, 10) for v in args.cores.split(',')]
    log.info("CPUs:         %s", ' '.join([str(v) for v in cores]))
    
    # Setup the socket, if needed
    isock = None
    if not args.offline:
        iaddr = Address(args.address, args.port)
        isock = UDPSocket()
        isock.bind(iaddr)
        isock.timeout = 11
        
    # Setup the rings
    capture_ring = Ring(name="capture", core=cores[0])
    
    # Setup antennas
    nant = len(station.antennas)
    nbl = nant*(nant+1)//2
    
    # Setup the recording directory, if needed
    if not os.path.exists(args.record_directory):
        status = os.system('mkdir -p %s' % args.record_directory)
        if status != 0:
            raise RuntimeError("Unable to create directory: %s" % args.record_directory)
    else:
        if not os.path.isdir(os.path.realpath(args.record_directory)):
            raise RuntimeError("Cannot record to a non-directory: %s" % args.record_directory)
            
    # Setup the blocks
    ops = []
    if args.offline:
        ops.append(DummyOp(log, isock, capture_ring, (NPIPELINE//16)*nbl,
                           ntime_gulp=args.gulp_size, slot_ntime=(600 if args.quick else 6),
                           fast=args.quick, core=cores.pop(0)))
    else:
        ops.append(CaptureOp(log, isock, capture_ring, (NPIPELINE//16)*nbl,   # two pipelines/recorder
                             ntime_gulp=args.gulp_size, slot_ntime=(600 if args.quick else 6),
                             fast=args.quick, core=cores.pop(0)))
    if not args.quick:
        ops.append(SpectraOp(log, mcs_id, capture_ring,
                             ntime_gulp=args.gulp_size, core=cores.pop(0)))
        ops.append(BaselineOp(log, mcs_id, station, capture_ring,
                              ntime_gulp=args.gulp_size, core=cores.pop(0)))
        if args.image:
            ops.append(ImageOp(log, mcs_id, station, capture_ring,
                               cal_dir=args.cal_dir, ntime_gulp=args.gulp_size,
                               core=cores.pop(0)))
    ops.append(StatisticsOp(log, mcs_id, capture_ring,
                            ntime_gulp=args.gulp_size, core=cores.pop(0)))
    ops.append(WriterOp(log, mcs_id, station, capture_ring,
                        ntime_gulp=args.gulp_size, fast=args.quick, core=cores.pop(0)))
    ops.append(GlobalLogger(log, mcs_id, args, QUEUE, quota=args.record_directory_quota,
                            threads=ops, gulp_time=args.gulp_size*2400*(1 if args.quick else 100)*(2*NCHAN/CLOCK),  # Ugh, hard coded
                            quota_mode='time'))
    ops.append(VisibilityCommandProcessor(log, mcs_id, args.record_directory, QUEUE,
                                          nint_per_file=args.nint_per_file,
                                          is_tarred=not args.no_tar))
    
    # Setup the threads
    threads = [threading.Thread(target=op.main, name=type(op).__name__) for op in ops]
    
    # Setup signal handling
    shutdown_event = setup_signal_handling(ops)
    ops[0].shutdown_event = shutdown_event
    ops[-2].shutdown_event = shutdown_event
    ops[-1].shutdown_event = shutdown_event
    
    # Launch!
    log.info("Launching %i thread(s)", len(threads))
    for thread in threads:
        #thread.daemon = True
        thread.start()
        
    while not shutdown_event.is_set():
        signal.pause()
    log.info("Shutdown, waiting for threads to join")
    for thread in threads:
        thread.join()
    log.info("All done")
    
    os.system(f"kill -9 {os.getpid()}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
    
