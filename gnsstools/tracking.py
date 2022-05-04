from abc import ABC, abstractmethod
from asyncio import constants
import configparser
import numpy as np

from gnsstools.gnsssignal import GNSSSignal
from gnsstools.rffile import RFFile
from gnsstools.acquisition import Acquisition
from gnsstools.rfsignal import RFSignal

class Tracking:

    def __init__(self, configfile, acquisition:Acquisition):
        config = configparser.ConfigParser()
        config.read(configfile)

        self.msToProcess = config.getint('DEFAULT', 'ms_to_process')
        
        self.pdiCode    = config.getfloat('TRACKING', 'pdi_code')
        self.pdiCarrier = config.getfloat('TRACKING', 'pdi_carrier')

        # Load tracking parameters
        self.dllCorrelatorSpacing = config.getfloat('TRACKING', 'dll_correlator_spacing')
        self.dllDumpingRatio      = config.getfloat('TRACKING', 'dll_dumping_ratio')
        self.dllNoiseBandwidth    = config.getfloat('TRACKING', 'dll_noise_bandwidth')
        self.dllLoopGain          = config.getfloat('TRACKING', 'dll_loop_gain')

        self.pllDumpingRatio      = config.getfloat('TRACKING', 'pll_dumping_ratio')
        self.pllNoiseBandwidth    = config.getfloat('TRACKING', 'pll_noise_bandwidth')
        self.pllLoopGain          = config.getfloat('TRACKING', 'pll_loop_gain')
        
        self.acquisition = acquisition
        self.prn         = self.acquisition.prn
        self.signal      = self.acquisition.signal
        self.init_freq   = self.acquisition.coarseFreq
        self.init_code   = self.acquisition.coarseCode

        self.absoluteSample   = []
        self.codeFrequency    = []
        self.carrierFrequency = []
        self.codeError        = []
        self.codeNCO          = []
        self.carrierError     = []
        self.carrierNCO       = []
        self.codePhase        = []   
        self.iEarly  = []
        self.qEarly  = []
        self.iPrompt = []
        self.qPrompt = []
        self.iLate   = []
        self.qLate   = []
        
        # Initialise
        self.dllTau1, self.dllTau2 = self.getLoopCoefficients(self.dllNoiseBandwidth, \
            self.dllDumpingRatio, self.dllLoopGain)
        self.pllTau1, self.pllTau2 = self.getLoopCoefficients(self.pllNoiseBandwidth, \
            self.pllDumpingRatio, self.pllLoopGain)

        return
    
    def getLoopCoefficients(self, loopNoiseBandwidth, dumpingRatio, loopGain):
        """
        Compute loop coeficients for PLL and DLL loops. From code of [Borre, 2007].

        Parameters
        ----------
        loopNoiseBandwidth : float
            Loop Noise Bandwith parameter
        dumpingRatio : float
            Dumping Ratio parameter, a.k.a. zeta
        loopGain
            Loop Gain parameter
        
        Returns
        -------
        tau1, tau2 : float, float
            Loop filter coeficients
        """

        Wn = loopNoiseBandwidth * 8.0 * dumpingRatio / \
            (4.0 * dumpingRatio**2 +1)
        
        tau1 = loopGain / Wn**2
        tau2 = 2.0 * dumpingRatio / Wn

        return tau1, tau2

    def track(self, signal_file:RFFile):
        """
        Track signal based on acquisition results. Largely inspired by [Borre, 2017]
        and the Python implementation from perrysou (Github).
        """

        # Generate CA
        caCode = self.signal.getCode(self.prn) # Could be stored in acquisition

        # Extend a bit the code with last and first value
        caCode = np.r_[caCode[-1], caCode, caCode[0]]

        # Initialize
        carrierFrequency = self.init_freq
        codeFrequency    = self.signal.code_freq
        remCarrierPhase  = 0.0 # Keep the remaining part of the carrier phase for next iteration
        remCodePhase     = 0.0 # Keep the remaining part of the code phase for next iteration
        codeNCO          = 0.0
        carrierNCO       = 0.0
        oldCodeError     = 0.0
        oldCarrierError  = 0.0

        # Start tracking
        for code_counter in range(self.msToProcess):
            # -----------------------------------------------------------------
            # Read signal from file
            codePhaseStep = codeFrequency / signal_file.samp_freq
            chunck = int(np.ceil((self.signal.code_bit - remCodePhase) / codePhaseStep))
            
            if code_counter == 0:
                skip = self.init_code + 1
                rawSignal = signal_file.readFileByValues(nb_values=chunck, skip=skip, keep_open=True)
            else:
                rawSignal = signal_file.readFileByValues(nb_values=chunck, keep_open=True)

            # Check if there is enough data in file
            if len(rawSignal) < chunck:
                raise EOFError("EOF encountered earlier than expected in file.")
            
            # -----------------------------------------------------------------
            # Generate the code replica for correlators
            ## Early code 
            idx = np.ceil(np.linspace(remCodePhase - self.dllCorrelatorSpacing, \
                chunck * codePhaseStep + remCodePhase - self.dllCorrelatorSpacing, \
                chunck, endpoint=False)).astype(int)
            
            earlyCode = caCode[idx]

            ## Late code
            idx = np.ceil(np.linspace(remCodePhase + self.dllCorrelatorSpacing, \
                chunck * codePhaseStep + remCodePhase + self.dllCorrelatorSpacing, \
                chunck, endpoint=False)).astype(int)

            lateCode = caCode[idx]

            ## Prompt code
            # We don't apply ceil nor int here because we need the float value
            # of the last index to compute the remaining phase
            idx = np.linspace(remCodePhase, \
                chunck * codePhaseStep + remCodePhase, \
                chunck, endpoint=False)
            
            promptCode = caCode[np.ceil(idx).astype(int)]

            # Update the remain code phase variable
            remCodePhase = idx[chunck-1] + codePhaseStep - self.signal.code_bit

            # -----------------------------------------------------------------
            # Generate carrier replica and mix to remove frequency shift
            # We use (chunck+1) and not (chunck) because we want one more to
            # estimate the remaining of the carrier phase
            time = np.arange(0, chunck+1) / signal_file.samp_freq
            #temp = carrierFrequency * 2.0 * np.pi * time + remCarrierPhase
            temp = -(carrierFrequency * 2.0 * np.pi * time) + remCarrierPhase

            remCarrierPhase = temp[chunck] % (2 * np.pi)
            
            carrierSignal = np.exp(1j * temp[:chunck]) * rawSignal
            iSignal = np.real(carrierSignal)
            qSignal = np.imag(carrierSignal)
            #iSignal = np.sin(temp[:chunck]) * rawSignal # In-phase
            #qSignal = np.cos(temp[:chunck]) * rawSignal # Quadraphase

            # -----------------------------------------------------------------
            # Correlators update
            iEarly  = np.sum(earlyCode  * iSignal)
            qEarly  = np.sum(earlyCode  * qSignal)
            iPrompt = np.sum(promptCode * iSignal)
            qPrompt = np.sum(promptCode * qSignal)
            iLate   = np.sum(lateCode   * iSignal)
            qLate   = np.sum(lateCode   * qSignal)
            
            # -----------------------------------------------------------------
            # DLL
            codeError = (np.sqrt(iEarly**2 + qEarly**2) - np.sqrt(iLate**2 + qLate**2)) / \
                (np.sqrt(iEarly**2 + qEarly**2) + np.sqrt(iLate**2 + qLate**2))
            
            # Update NCO code
            codeNCO += self.dllTau2 / self.dllTau1 * (codeError - oldCodeError) \
                + codeError * (self.pdiCode / self.dllTau1)
            
            oldCodeError = codeError

            codeFrequency = self.signal.code_freq - codeNCO

            # -----------------------------------------------------------------
            # PLL
            carrierError = np.arctan(qPrompt / iPrompt) / 2.0 / np.pi

            # Update NCO carrier
            carrierNCO += self.pllTau2 / self.pllTau1 * (carrierError - oldCarrierError) \
                + carrierError * (self.pdiCarrier / self.pllTau1)

            oldCarrierError = carrierError

            carrierFrequency = self.init_freq + carrierNCO

            # -----------------------------------------------------------------
            # FLL
            # TODO

            # Save variables
            self.absoluteSample.append(signal_file.getCurrentSampleIndex())
            self.codeFrequency.append(codeFrequency)
            self.carrierFrequency.append(carrierFrequency)
            self.codeError.append(codeError)
            self.codeNCO.append(codeNCO)
            self.carrierError.append(carrierError)
            self.carrierNCO .append(carrierNCO)
            self.iEarly.append(iEarly)
            self.qEarly.append(qEarly)
            self.iPrompt.append(iPrompt)
            self.qPrompt.append(qPrompt)
            self.iLate.append(iLate)
            self.qLate.append(qLate)
            self.codePhase.append(remCodePhase)

        # TODO Should pre-allocate all the arrays and put this in numpy arrays right away
        self.iPrompt = np.array(self.iPrompt)

        signal_file.closeFile()

        return


class TrackingAbstract(ABC):

    @abstractmethod
    def __init__(self, rfConfig:RFSignal, signalConfig:GNSSSignal):
        self.rfConfig  = rfConfig
        self.signalConfig = signalConfig

        self.remCodePhase     = 0.0
        self.remCarrierPhase  = 0.0
        self.codePhaseStep    = 0.0
        self.codeFrequency    = 0.0
        self.codeNCO          = 0.0
        self.codeError        = 0.0
        self.initialFrequency = 0.0
        self.carrierFrequency = 0.0
        self.carrierNCO       = 0.0
        self.carrierError     = 0.0
        
        self.code    = []
        self.iSignal = []
        self.qSignal = []

        self.codeFrequency   = self.signalConfig.codeFrequency
        self.codePhaseStep   = self.signalConfig.codeFrequency / self.rfConfig.samplingFrequency
        self.samplesRequired = int(np.ceil((self.signalConfig.codeBits - self.remCodePhase) / self.codePhaseStep))

        self.correlatorSpacing = []
        self.correlatorResults = []
    
    # -------------------------------------------------------------------------
    # ACSTRACT METHODS
    
    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def delayLockLoop(self):
        pass

    @abstractmethod
    def phaseLockLoop(self):
        pass

    # -------------------------------------------------------------------------
    # METHODS

    def getCorrelator(self, correlatorSpacing):

        idx = np.ceil(np.linspace(self.remCodePhase + correlatorSpacing, \
                self.samplesRequired * self.codePhaseStep + self.remCodePhase + correlatorSpacing, \
                self.samplesRequired, endpoint=False)).astype(int)
        tmpCode = self.code[idx]

        iCorr  = np.sum(tmpCode  * self.iSignal)
        qCorr  = np.sum(tmpCode  * self.qSignal)

        return iCorr, qCorr

    # -------------------------------------------------------------------------
    
    def setInitialValues(self, estimatedFrequency):
        self.initialFrequency = estimatedFrequency
        self.carrierFrequency = estimatedFrequency
        return
    # -------------------------------------------------------------------------

    def setSatellite(self, svid):
        self.svid = svid
        code = self.signalConfig.getCode(svid)
        self.code = np.r_[code[-1], code, code[0]]
        
        return 

    
    # -------------------------------------------------------------------------
    # GETTER / SETTER

    def getSamplesRequired(self):
        return self.samplesRequired

class Tracking_EPL(TrackingAbstract):

    def __init__(self, rfConfig:RFSignal, signalConfig:GNSSSignal):
        super().__init__(rfConfig, signalConfig)

        config = configparser.ConfigParser()
        config.read(self.signalConfig.configFile)

        self.pdiCode    = config.getfloat('TRACKING', 'pdi_code')
        self.pdiCarrier = config.getfloat('TRACKING', 'pdi_carrier')

        self.correlatorSpacing = config.getfloat('TRACKING', 'correlator_spacing')

        self.dllDumpingRatio   = config.getfloat('TRACKING', 'dll_dumping_ratio')
        self.dllNoiseBandwidth = config.getfloat('TRACKING', 'dll_noise_bandwidth')
        self.dllLoopGain       = config.getfloat('TRACKING', 'dll_loop_gain')

        self.pllDumpingRatio   = config.getfloat('TRACKING', 'pll_dumping_ratio')
        self.pllNoiseBandwidth = config.getfloat('TRACKING', 'pll_noise_bandwidth')
        self.pllLoopGain       = config.getfloat('TRACKING', 'pll_loop_gain')
        
        self.dllTau1, self.dllTau2 = self.getLoopCoefficients(self.dllNoiseBandwidth, \
            self.dllDumpingRatio, self.dllLoopGain)
        self.pllTau1, self.pllTau2 = self.getLoopCoefficients(self.pllNoiseBandwidth, \
            self.pllDumpingRatio, self.pllLoopGain)

        self.correlatorSpacing = [-self.correlatorSpacing, 0.0, self.correlatorSpacing]
        return

    def run(self, rfData):

        # Generate replica and mix signal
        time = np.arange(0, self.samplesRequired+1) / self.rfConfig.samplingFrequency
        temp = -(self.carrierFrequency * 2.0 * np.pi * time) + self.remCarrierPhase

        self.remCarrierPhase = temp[self.samplesRequired] % (2 * np.pi)
        
        carrierSignal = np.exp(1j * temp[:self.samplesRequired]) * rfData
        self.iSignal = np.real(carrierSignal)
        self.qSignal = np.imag(carrierSignal)

        # Build correlators (Early-Prompt-Late)
        iEarly , qEarly  = self.getCorrelator(self.correlatorSpacing[0])
        iPrompt, qPrompt = self.getCorrelator(self.correlatorSpacing[1])
        iLate  , qLate   = self.getCorrelator(self.correlatorSpacing[2])

        self.correlatorResults = [iEarly, qEarly, iPrompt, qPrompt, iLate, qLate]
        
        # Delay Lock Loop (DLL)
        self.delayLockLoop(iEarly, qEarly, iLate, qLate)
        
        # Phase Lock Loop (PLL)
        self.phaseLockLoop(iPrompt, qPrompt)

        # Get remaining phase
        idx = np.linspace(self.remCodePhase, self.samplesRequired * self.codePhaseStep + self.remCodePhase, \
                          self.samplesRequired, endpoint=False)
        self.remCodePhase = idx[self.samplesRequired-1] + self.codePhaseStep - self.signalConfig.codeBits

        self.codePhaseStep = self.codeFrequency / self.rfConfig.samplingFrequency
        self.samplesRequired = int(np.ceil((self.signalConfig.codeBits - self.remCodePhase) / self.codePhaseStep))

        return
    
    def delayLockLoop(self, iEarly, qEarly, iLate, qLate):

        newCodeError = (np.sqrt(iEarly**2 + qEarly**2) - np.sqrt(iLate**2 + qLate**2)) / \
                    (np.sqrt(iEarly**2 + qEarly**2) + np.sqrt(iLate**2 + qLate**2))
            
        # Update NCO code
        self.codeNCO += self.dllTau2 / self.dllTau1 * (newCodeError - self.codeError)
        self.codeNCO += self.pdiCode / self.dllTau1 * newCodeError
        
        self.codeError = newCodeError
        self.codeFrequency = self.signalConfig.codeFrequency - self.codeNCO

        return

    def phaseLockLoop(self, iPrompt, qPrompt):

        newCarrierError = np.arctan(qPrompt / iPrompt) / 2.0 / np.pi

        # Update NCO frequency
        self.carrierNCO += self.pllTau2 / self.pllTau1 * (newCarrierError - self.carrierError)
        self.carrierNCO += self.pdiCarrier / self.pllTau1 * newCarrierError

        self.carrierError = newCarrierError
        self.carrierFrequency = self.initialFrequency + self.carrierNCO

        return
    
    def getLoopCoefficients(self, loopNoiseBandwidth, dumpingRatio, loopGain):
        """
        Compute loop coeficients for PLL and DLL loops. From code of [Borre, 2007].

        Args:
            loopNoiseBandwidth (float): Loop Noise Bandwith parameter
            dumpingRatio (float): Dumping Ratio parameter, a.k.a. zeta
            loopGain (float): Loop Gain parameter
        
        Returns
            tau1 (float): Loop filter coeficient (1st)
            tau2 (float): Loop filter coeficient (2nd)

        Raises:
            None
        """

        Wn = loopNoiseBandwidth * 8.0 * dumpingRatio / \
            (4.0 * dumpingRatio**2 +1)
        
        tau1 = loopGain / Wn**2
        tau2 = 2.0 * dumpingRatio / Wn

        return tau1, tau2

